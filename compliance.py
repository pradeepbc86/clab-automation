#!/usr/bin/env python3
"""
Compliance / drift detection: compare device running config to SoT-rendered intent.

Per device:
  1. Pull running config (RANCID dump or live `docker exec vtysh`)
  2. Re-render intended config from SoT via generate.py
  3. Normalize both (strip noise lines, lowercase, collapse whitespace)
  4. Diff via difflib.unified_diff
  5. Emit a structured drift record (obs_sink) so observability can ingest
  6. Exit non-zero if --fail-on-drift and any device drifts;
     exit 2 if any device exceeds --max-drift-lines threshold
"""

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

from generate import generate_configs, get_devices_from_netbox, validate_devices

DRIFT_LOG = Path(os.getenv("COMPLIANCE_DRIFT_LOG", "compliance-drift.jsonl"))
RANCID_DIR = Path(os.getenv("RANCID_DIR", "/var/lib/rancid/fabric/configs"))


# Lines to strip before comparing — vendor banners, timestamps, route counts
# that fluctuate without semantic change.
_NOISE_PATTERNS = [
    r"^!.*$",                             # FRR/EOS comment
    r"^\s*$",                             # blank
    r"^Building configuration\.\.\.",     # EOS banner
    r"^Current configuration : \d+",      # EOS metadata
    r"^\s*Last configuration change",     # Junos
    r"^version \d+\.\d+\.\d+",            # version strings
    r"^!Time:",                           # timestamps
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS))


def normalize(config: str) -> list[str]:
    out = []
    for line in config.splitlines():
        stripped = line.rstrip()
        if _NOISE_RE.match(stripped):
            continue
        out.append(re.sub(r"\s+", " ", stripped).lower())
    return out


def fetch_running_config(device_name: str) -> str:
    """RANCID dump if available, otherwise live SSH via clab container exec."""
    rancid_path = RANCID_DIR / device_name
    if rancid_path.exists():
        return rancid_path.read_text()
    cmd = ["docker", "exec", f"clab-automation-{device_name}",
           "vtysh", "-c", "show running-config"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.stdout
    except Exception as e:
        return f"# fetch failed: {e}\n"


def get_intended_config(device_name: str) -> str:
    out = Path(f"output/{device_name}.conf")
    if not out.exists():
        generate_configs(device_name)
    return out.read_text() if out.exists() else ""


def diff_device(device_name: str, max_drift_lines: int = 20) -> dict:
    intended = normalize(get_intended_config(device_name))
    actual = normalize(fetch_running_config(device_name))

    diff = list(
        difflib.unified_diff(
            actual, intended,
            fromfile=f"{device_name}.running",
            tofile=f"{device_name}.intended",
            lineterm="",
        )
    )
    drift_lines = [
        d for d in diff
        if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))
    ]
    return {
        "device": device_name,
        "drift_line_count": len(drift_lines),
        "compliant": len(drift_lines) == 0,
        "over_threshold": len(drift_lines) > max_drift_lines,
        "diff": "\n".join(diff[:500]),  # cap for record size
    }


def emit_drift_record(record: dict):
    """Emit drift record via obs_sink (JSONL default + optional ES forward)."""
    from obs_sink import emit as _sink_emit
    record.setdefault("source", "compliance.py")
    _sink_emit(record, path=DRIFT_LOG)


def main():
    parser = argparse.ArgumentParser(description="Detect config drift vs SoT")
    parser.add_argument("--device", help="Single device (default: all)")
    parser.add_argument("--max-drift-lines", type=int, default=20,
                        help="Lines of drift before flagging over-threshold")
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()

    devices_data = get_devices_from_netbox()
    devices_data = validate_devices(devices_data) if devices_data else []
    devices = [d["name"] for d in devices_data] or ["spine1", "leaf1", "leaf2"]
    if args.device:
        devices = [args.device]

    print(f"=== Compliance check: {len(devices)} device(s) ===\n")

    any_drift = False
    over_threshold = False
    for device in devices:
        record = diff_device(device, args.max_drift_lines)
        emit_drift_record(record)
        if record["compliant"]:
            print(f"✅ {device}: compliant")
        else:
            mark = "❌" if record["over_threshold"] else "⚠️"
            extra = " (OVER threshold)" if record["over_threshold"] else ""
            print(f"{mark} {device}: {record['drift_line_count']} drift lines{extra}")
            any_drift = True
            over_threshold |= record["over_threshold"]

    print(f"\nDrift records written to {DRIFT_LOG}")
    if args.fail_on_drift and any_drift:
        sys.exit(1)
    if over_threshold:
        sys.exit(2)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
