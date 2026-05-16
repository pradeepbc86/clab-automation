"""
obs_sink — structured event emission helper.

Default sink: append to a JSONL file on the local filesystem.
Optional sinks: forward to Elasticsearch / pushgateway via env-var URLs.

Used by deploy.py, compliance.py, controller.py to emit deploy events,
drift detections, reconciliation outcomes. Same shape goes to all sinks
so a downstream Vector/Fluent Bit pipeline can forward JSONL → ES in
production without touching the emitting code.

Env vars:
  OBS_SINK_FILE         — path to local JSONL (default: events.jsonl)
  OBS_SINK_ES_URL       — if set, also POST events to {url}/{index}/_doc
  OBS_SINK_ES_INDEX     — ES index name (default: clab-automation-events)
  OBS_SINK_DISABLE_FILE — set to "true" to skip local file emission
"""

import json
import os
import time
import urllib.request
from pathlib import Path

DEFAULT_PATH = Path(os.getenv("OBS_SINK_FILE", "events.jsonl"))
ES_URL = os.getenv("OBS_SINK_ES_URL")
ES_INDEX = os.getenv("OBS_SINK_ES_INDEX", "clab-automation-events")
DISABLE_FILE = os.getenv("OBS_SINK_DISABLE_FILE", "false").lower() == "true"


def emit(record: dict, *, path: Path | None = None):
    """
    Write one structured event to all configured sinks.

    Always stamps `ts` (UTC ISO8601). Never raises — sink failures are logged
    to stderr but don't crash the caller. The pipeline must keep running even
    when the obs stack is partially down.
    """
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Sink 1: local JSONL (always available, durable across obs-stack outages)
    target = path or DEFAULT_PATH
    if not DISABLE_FILE:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"[obs_sink] file write failed: {e}", flush=True)

    # Sink 2: Elasticsearch (when wired)
    if ES_URL:
        try:
            req = urllib.request.Request(
                f"{ES_URL.rstrip('/')}/{ES_INDEX}/_doc",
                data=json.dumps(record).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"[obs_sink] ES post failed (continuing): {e}", flush=True)
