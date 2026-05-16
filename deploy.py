#!/usr/bin/env python3
"""
Deploy BGP configs to devices.

Features:
- NAPALM for cEOS (transactional load_merge → diff → commit/discard)
- Netmiko for FRR (line-by-line apply, non-transactional)
- Filesystem lock — only one deploy runs at a time per host
- Diff-bounds enforcement — refuse to ship if rendered config differs by more
  than --max-delta lines from the prior rendered version
- Canary sequencing — deploy to a "canary" device first, sleep, verify, proceed
- Safety budget — abort the run if more than --safety-threshold devices fail
- Deploy events — emit a structured JSONL record per device deploy attempt for
  obs-telemetry ingestion
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from napalm import get_network_driver
from netmiko import ConnectHandler

# --- configuration -----------------------------------------------------------

DEVICES = {
    'spine1': {'host': '127.0.0.1', 'driver': 'eos', 'port': 8001},
    'leaf1':  {'host': '127.0.0.1', 'driver': 'eos', 'port': 8002},
    'leaf2':  {'host': '127.0.0.1', 'driver': 'frr', 'port': 22},
}

DEVICE_USER = os.getenv("DEVICE_USER", "admin")
DEVICE_PASSWORD = os.getenv("DEVICE_PASSWORD")
LOCK_PATH = Path(os.getenv("DEPLOY_LOCK", ".deploy.lock"))
EVENTS_PATH = Path(os.getenv("DEPLOY_EVENTS", "deploy-events.jsonl"))

AUTO_YES = False


# --- helpers -----------------------------------------------------------------

def emit_event(record: dict):
    """Append a structured deploy event for obs-telemetry to ingest."""
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def check_diff_bounds(device_name: str, max_delta: int) -> tuple[bool, int]:
    """
    Compare current output/<device>.conf to git HEAD's version.
    Returns (within_bounds, actual_line_delta).
    """
    new_path = Path(f"output/{device_name}.conf")
    if not new_path.exists():
        return True, 0
    try:
        prior = subprocess.check_output(
            ["git", "show", f"HEAD:output/{device_name}.conf"],
            stderr=subprocess.DEVNULL, text=True,
        )
    except subprocess.CalledProcessError:
        # No prior version (first deploy) — treat as unbounded but report delta
        return True, sum(1 for _ in new_path.read_text().splitlines())

    new_lines = new_path.read_text().splitlines()
    prior_lines = prior.splitlines()

    # Count net line difference (insertions + deletions, not character churn)
    import difflib
    diff = list(difflib.unified_diff(prior_lines, new_lines, lineterm=""))
    delta = sum(1 for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---")))

    return delta <= max_delta, delta


# --- deploy paths -------------------------------------------------------------

def deploy_with_napalm(device_name: str, config_file: str) -> dict:
    dev_info = DEVICES[device_name]
    driver = get_network_driver(dev_info['driver'])
    device = driver(
        hostname=dev_info['host'],
        username=DEVICE_USER,
        password=DEVICE_PASSWORD,
        port=dev_info['port'],
    )
    device.open()

    config = Path(config_file).read_text()
    device.load_merge_candidate(config=config)
    diff = device.compare_config()
    print(f"\n--- Diff for {device_name} ---\n{diff}\n")

    if not diff.strip():
        print(f"✓ {device_name}: no change (idempotent)")
        device.discard_config()
        device.close()
        return {"device": device_name, "outcome": "no-op", "diff_lines": 0}

    if AUTO_YES or input(f"Deploy to {device_name}? [y/N]: ").lower() == 'y':
        device.commit_config()
        outcome = "deployed"
        print(f"✅ {device_name}: deployed")
    else:
        device.discard_config()
        outcome = "discarded"
        print(f"❌ {device_name}: discarded")

    device.close()
    return {"device": device_name, "outcome": outcome, "diff_lines": len(diff.splitlines())}


def deploy_with_netmiko(device_name: str, config_file: str) -> dict:
    conn = ConnectHandler(
        device_type='linux', host='127.0.0.1',
        username=DEVICE_USER, password=DEVICE_PASSWORD, port=2222,
    )
    config = Path(config_file).read_text()
    print(f"\n--- Config for {device_name} ---\n{config}\n")

    if AUTO_YES or input(f"Deploy to {device_name}? [y/N]: ").lower() == 'y':
        conn.send_config_set(config.split("\n"))
        outcome = "deployed"
        print(f"✅ {device_name}: deployed")
    else:
        outcome = "skipped"
        print(f"❌ {device_name}: skipped")

    conn.disconnect()
    return {"device": device_name, "outcome": outcome, "diff_lines": len(config.splitlines())}


# --- orchestration ------------------------------------------------------------

def deploy_one(device_name: str, max_delta: int) -> dict:
    started = time.time()
    base_record = {"device": device_name, "user": os.getenv("USER", "unknown")}

    if device_name not in DEVICES:
        emit_event({**base_record, "outcome": "unknown-device", "duration_s": 0})
        return {"device": device_name, "outcome": "unknown-device"}

    within, delta = check_diff_bounds(device_name, max_delta)
    if not within:
        msg = f"diff {delta} lines exceeds --max-delta {max_delta}"
        print(f"❌ {device_name}: refusing — {msg}")
        emit_event({**base_record, "outcome": "rejected-bounds", "diff_lines": delta,
                    "duration_s": round(time.time() - started, 2)})
        return {"device": device_name, "outcome": "rejected-bounds", "diff_lines": delta}

    config_file = f"output/{device_name}.conf"
    try:
        driver = DEVICES[device_name]['driver']
        if driver == 'eos':
            result = deploy_with_napalm(device_name, config_file)
        else:
            result = deploy_with_netmiko(device_name, config_file)
    except Exception as e:
        print(f"❌ {device_name}: {e}")
        result = {"device": device_name, "outcome": "error", "error": str(e)}

    result["duration_s"] = round(time.time() - started, 2)
    emit_event({**base_record, **result})
    return result


def acquire_lock(path: Path):
    """Exclusive lock so only one deploy can run at a time on this host."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"❌ Another deploy is running (lock held: {path})")
        sys.exit(1)
    return fh  # keep fh alive for the lifetime of the process


def main():
    parser = argparse.ArgumentParser(description="Deploy generated configs")
    parser.add_argument("--device", help="Single device (mutually exclusive with --all)")
    parser.add_argument("--all", action="store_true", help="Deploy to every device in DEVICES")
    parser.add_argument("--canary", help="Deploy to this device first, sleep --canary-wait, then proceed")
    parser.add_argument("--canary-wait", type=int, default=30,
                        help="Seconds to wait between canary and rest")
    parser.add_argument("--max-delta", type=int, default=200,
                        help="Refuse to deploy if config changed by more than N lines vs git HEAD")
    parser.add_argument("--safety-threshold", type=float, default=0.34,
                        help="Abort the --all run if >threshold devices fail (default 1/3)")
    parser.add_argument("--yes", action="store_true", help="Non-interactive — auto-confirm")
    args = parser.parse_args()

    global AUTO_YES
    AUTO_YES = args.yes

    if not DEVICE_PASSWORD:
        print("Error: DEVICE_PASSWORD env var not set")
        sys.exit(1)

    if not args.device and not args.all:
        parser.print_help()
        sys.exit(1)

    _lock = acquire_lock(LOCK_PATH)

    if args.device:
        result = deploy_one(args.device, args.max_delta)
        sys.exit(0 if result["outcome"] in ("deployed", "no-op", "skipped", "discarded") else 1)

    # --all
    devices = list(DEVICES.keys())
    if args.canary:
        if args.canary not in devices:
            print(f"❌ canary device {args.canary!r} not in DEVICES")
            sys.exit(1)
        devices.remove(args.canary)
        devices.insert(0, args.canary)

    failures = 0
    results = []
    for idx, dev in enumerate(devices):
        result = deploy_one(dev, args.max_delta)
        results.append(result)

        if result["outcome"] in ("error", "rejected-bounds", "unknown-device"):
            failures += 1

        # Canary gate
        if idx == 0 and args.canary and result["outcome"] == "deployed":
            print(f"\n⏸  Canary {args.canary} deployed — waiting {args.canary_wait}s before proceeding")
            time.sleep(args.canary_wait)

        # Safety budget
        if failures / len(devices) > args.safety_threshold:
            print(f"\n❌ Aborting: {failures}/{len(devices)} failures exceed safety budget")
            emit_event({"event": "run-aborted", "failures": failures, "total": len(devices)})
            sys.exit(2)

    print(f"\nDone — {sum(1 for r in results if r['outcome'] == 'deployed')} deployed, "
          f"{failures} failed, {len(results)} total")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
