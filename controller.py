#!/usr/bin/env python3
"""
Reconciliation controller — pull-based, continuous fabric convergence loop.

Inspired by the Kubernetes controller pattern: instead of push-on-change CI/CD,
periodically pull running state, compute drift against intent (from NetBox SoT),
and emit drift events. Optionally trigger HITL-gated remediation.

Loop:
  while True:
    for device in fabric:
        running = fetch_running_config(device)         # RANCID / SSH
        intended = render_intent(device)               # generate.py
        drift = diff(running, intended)
        emit_event(device, drift)                      # → observability ES
        if drift.severity == "minor": auto_remediate(device)
        if drift.severity == "major": file_approval(device, drift)
    sleep(reconcile_interval)

This separates *detection* (compliance.py runs once) from *convergence*
(controller runs forever). Together they form a closed loop.

Run as a long-lived service. In a real fleet this would be a k8s Deployment
with `restartPolicy: Always` or a systemd unit.
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

# Reuse the pieces from compliance.py
from compliance import (
    diff_device,
    emit_drift_record,
    DRIFT_LOG,
)
from generate import get_devices_from_netbox, validate_devices

EVENTS_PATH = Path(os.getenv("CONTROLLER_EVENTS", "controller-events.jsonl"))
APPROVALS_DIR = Path(os.getenv("APPROVALS_DIR", "approvals"))

# Drift severity thresholds — drives action.
SEVERITY_MINOR_MAX_LINES = 5    # cosmetic / acceptable drift, auto-remediate ok
SEVERITY_MAJOR_THRESHOLD = 50   # >this lines triggers HITL approval

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    print(f"\n[controller] received signal {signum}, finishing iteration then stopping")
    _shutdown = True


def emit_controller_event(record: dict):
    """Emit controller event via obs_sink (JSONL default + optional ES forward)."""
    from obs_sink import emit as _sink_emit
    record.setdefault("source", "controller.py")
    _sink_emit(record, path=EVENTS_PATH)


def file_hitl_approval(device: str, drift_record: dict) -> str:
    """
    Stage a major-drift remediation for human approval.
    Mirrors the HITL primitive in clab-ai-mcp/tools/hitl.py.
    """
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    approval_id = str(uuid.uuid4())
    path = APPROVALS_DIR / f"{approval_id}.json"
    path.write_text(json.dumps({
        "id": approval_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "pending",
        "summary": f"{device}: drift {drift_record['drift_line_count']} lines",
        "target": device,
        "impact": "major",
        "proposed_by": "controller",
        "diff": drift_record.get("diff", ""),
    }, indent=2))
    return approval_id


def classify(drift_record: dict) -> str:
    """Map drift size to severity tier."""
    n = drift_record["drift_line_count"]
    if n == 0:
        return "converged"
    if n <= SEVERITY_MINOR_MAX_LINES:
        return "minor"
    if n <= SEVERITY_MAJOR_THRESHOLD:
        return "moderate"
    return "major"


def reconcile_once(devices: list[str], max_drift_lines: int):
    """One pass over the fleet. Records events, queues approvals where needed."""
    iteration_start = time.time()
    summary = {"converged": 0, "minor": 0, "moderate": 0, "major": 0}

    for device in devices:
        record = diff_device(device, max_drift_lines)
        emit_drift_record(record)
        sev = classify(record)
        summary[sev] += 1

        event = {
            "event": "reconcile_check",
            "device": device,
            "severity": sev,
            "drift_line_count": record["drift_line_count"],
        }

        if sev == "major":
            approval_id = file_hitl_approval(device, record)
            event["action"] = "filed_approval"
            event["approval_id"] = approval_id
            print(f"  ⚠ {device}: major drift ({record['drift_line_count']} lines) — approval {approval_id} filed")
        elif sev == "moderate":
            event["action"] = "logged_for_review"
            print(f"  · {device}: moderate drift ({record['drift_line_count']} lines)")
        elif sev == "minor":
            event["action"] = "auto_remediable"
            print(f"  · {device}: minor drift ({record['drift_line_count']} lines)")
        else:
            event["action"] = "no_op"
            print(f"  ✓ {device}: converged")

        emit_controller_event(event)

    duration = time.time() - iteration_start
    emit_controller_event({
        "event": "reconcile_iteration_complete",
        "duration_s": round(duration, 2),
        **summary,
    })
    return summary


def main():
    parser = argparse.ArgumentParser(description="Pull-based fabric reconciliation")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between reconcile passes (default 300)")
    parser.add_argument("--max-drift-lines", type=int, default=200,
                        help="Drift line cap before classifying as major")
    parser.add_argument("--once", action="store_true",
                        help="Run a single reconcile pass and exit")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Cache SoT fetch — re-fetched only every NETBOX_REFRESH_SECONDS, not every iteration.
    # Real fleet would invalidate on NetBox webhook; here we just TTL-cache.
    NETBOX_REFRESH_SECONDS = int(os.getenv("NETBOX_REFRESH_SECONDS", "1800"))  # 30 min
    devices_cache: list[str] = []
    devices_cache_time: float = 0.0

    def get_devices():
        nonlocal devices_cache, devices_cache_time
        if not devices_cache or time.time() - devices_cache_time > NETBOX_REFRESH_SECONDS:
            data = get_devices_from_netbox()
            data = validate_devices(data) if data else []
            devices_cache = [d["name"] for d in data] or ["spine1", "leaf1", "leaf2"]
            devices_cache_time = time.time()
            print(f"[controller] refreshed device list from NetBox: {devices_cache}")
        return devices_cache

    initial = get_devices()
    print(f"[controller] starting — {len(initial)} device(s), interval={args.interval}s, "
          f"netbox refresh {NETBOX_REFRESH_SECONDS}s")

    iteration = 0
    while not _shutdown:
        iteration += 1
        print(f"\n[controller] iteration #{iteration} at {time.strftime('%H:%M:%S')}")
        try:
            summary = reconcile_once(get_devices(), args.max_drift_lines)
            print(f"[controller] iteration done — {summary}")
        except Exception as e:
            print(f"[controller] iteration error: {e}")
            emit_controller_event({"event": "iteration_error", "error": str(e)})

        if args.once:
            break

        for _ in range(args.interval):
            if _shutdown:
                break
            time.sleep(1)

    print("[controller] stopped")


if __name__ == "__main__":
    main()
