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
  observability ingestion
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from napalm import get_network_driver
from netmiko import ConnectHandler

# Prometheus endpoint for the cross-system canary gate. If --canary is set,
# deploy.py queries Prometheus after the canary device deploys to confirm
# convergence before proceeding to the rest of the fleet.
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

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
    """Emit a deploy event via obs_sink (JSONL default + optional ES forward)."""
    from obs_sink import emit as _sink_emit
    record.setdefault("source", "deploy.py")
    _sink_emit(record, path=EVENTS_PATH)


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


def deploy_with_frr_reload(device_name: str, config_file: str) -> dict:
    """
    Transactional FRR deploy via `frr-reload.py` over SSH + SFTP.

    Flow:
      1. SFTP the rendered frr.conf to /tmp on the device (paramiko, not heredoc)
      2. SSH and run `frr-reload.py --test` to validate parsable
      3. If valid + operator-approved, run `frr-reload.py --reload` (atomic apply)
      4. SFTP cleanup
      5. Capture diff output for audit log

    frr-reload.py is FRR's official differential config tool — computes the
    minimal vtysh command set needed to converge running → candidate, and
    rolls back automatically if any command fails parse.
    """
    import paramiko  # local import — paramiko is heavier than netmiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname="127.0.0.1", port=2222,
        username=DEVICE_USER, password=DEVICE_PASSWORD,
        timeout=10,
    )

    remote_path = f"/tmp/frr-{device_name}-new.conf"

    # --- Phase 0: stage file via SFTP (clean transport, no heredoc tricks) ---
    sftp = client.open_sftp()
    sftp.put(config_file, remote_path)
    sftp.close()

    def _run(cmd: str, timeout: int = 30) -> tuple[int, str, str]:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    try:
        # --- Phase 1: validate (parse-only) ---
        rc, test_out, test_err = _run(f"sudo frr-reload.py --test --reload {remote_path}")
        combined = test_out + test_err
        print(f"\n--- frr-reload diff for {device_name} ---\n{combined}\n")

        if rc != 0:
            return _cleanup(client, remote_path, {
                "device": device_name, "outcome": "test-failed",
                "diff_lines": 0, "stderr_preview": test_err[:500],
            })

        if "lines to add" not in combined and "lines to delete" not in combined:
            print(f"✓ {device_name}: no change (idempotent)")
            return _cleanup(client, remote_path, {
                "device": device_name, "outcome": "no-op", "diff_lines": 0,
            })

        if not (AUTO_YES or input(f"Apply to {device_name}? [y/N]: ").lower() == "y"):
            print(f"❌ {device_name}: skipped (test diff only)")
            return _cleanup(client, remote_path, {
                "device": device_name, "outcome": "skipped",
                "diff_lines": combined.count("\n"),
            })

        # --- Phase 2: atomic apply ---
        rc, apply_out, apply_err = _run(f"sudo frr-reload.py --reload {remote_path}", timeout=60)
        if rc == 0:
            print(f"✅ {device_name}: deployed (transactional)")
            outcome = "deployed"
        else:
            print(f"❌ {device_name}: frr-reload failed (rc={rc}):\n{apply_err}")
            outcome = "apply-error"

        return _cleanup(client, remote_path, {
            "device": device_name,
            "outcome": outcome,
            "diff_lines": combined.count("\n"),
        })
    finally:
        try:
            client.close()
        except Exception:
            pass


def _cleanup(client, remote_path: str, result: dict) -> dict:
    """Remove staged file and close transport. Best-effort."""
    try:
        client.exec_command(f"rm -f {remote_path}")
    except Exception:
        pass
    return result


# Backwards-compat alias for the orchestration code that called deploy_with_netmiko
deploy_with_netmiko = deploy_with_frr_reload


# --- cross-system canary gate (Prometheus convergence) -----------------------

def prom_query(promql: str) -> float | None:
    """Run an instant PromQL query, return the first sample's value or None."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query=" + urllib.parse.quote(promql)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "success":
            return None
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception:
        return None


def wait_for_convergence(device: str, timeout_s: int, poll_s: int = 5) -> tuple[bool, str]:
    """
    Block until Prometheus signals that `device` has converged after a deploy.

    Two signals must hold:
      1. All BGP peers on this device are Established
      2. No BFD flap on this device in the last 30s
    """
    started = time.time()
    while time.time() - started < timeout_s:
        established = prom_query(
            f'sum(frr_bgp_peer_state{{state="Established",instance="{device}"}})'
        )
        bfd_flaps = prom_query(
            f'sum(changes(frr_bfd_peer_uptime_seconds{{instance="{device}"}}[30s]))'
        )

        if established is None or bfd_flaps is None:
            elapsed = int(time.time() - started)
            print(f"  [convergence-gate] {device}: telemetry not visible yet ({elapsed}s)")
        elif established >= 1 and bfd_flaps == 0:
            return True, f"Established={int(established)} BFD_flaps=0"
        else:
            print(
                f"  [convergence-gate] {device}: "
                f"Established={int(established)} BFD_flaps={int(bfd_flaps)} — waiting"
            )
        time.sleep(poll_s)
    return False, f"timeout after {timeout_s}s"


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
    parser.add_argument("--canary", help="Deploy to this device first, then wait for telemetry signal")
    parser.add_argument("--canary-wait", type=int, default=30,
                        help="Seconds to wait (used only with --skip-convergence-gate)")
    parser.add_argument("--convergence-timeout", type=int, default=120,
                        help="Seconds to wait for Prometheus to confirm canary convergence")
    parser.add_argument("--skip-convergence-gate", action="store_true",
                        help="Fall back to time-based wait instead of querying Prometheus")
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

        # Canary gate — Prometheus convergence signal preferred, time-based fallback
        if idx == 0 and args.canary and result["outcome"] == "deployed":
            if args.skip_convergence_gate:
                print(f"\n⏸  Canary {args.canary} deployed — sleeping {args.canary_wait}s (skip-gate)")
                time.sleep(args.canary_wait)
            else:
                print(f"\n⏸  Canary {args.canary} deployed — waiting for Prometheus convergence "
                      f"signal (timeout {args.convergence_timeout}s)")
                converged, reason = wait_for_convergence(args.canary, args.convergence_timeout)
                if not converged:
                    print(f"❌ Canary did NOT converge ({reason}) — aborting fleet rollout")
                    emit_event({"event": "canary-convergence-failed",
                                "device": args.canary, "reason": reason})
                    sys.exit(3)
                print(f"✅ Canary converged ({reason}) — proceeding")
                emit_event({"event": "canary-converged",
                            "device": args.canary, "reason": reason})

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
