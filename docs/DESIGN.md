# clab-automation — Design Document

> **Audience:** A network engineer who has configured BGP/EVPN on vendor gear by hand but hasn't done GitOps-style network automation before. You know what NetBox, RANCID, NAPALM, Netmiko, and Jinja2 *are*; this doc explains how they fit together into a coherent pipeline and how the Python orchestration glues it all.

---

## 1. What this repo is

A **declarative multi-vendor BGP/EVPN configuration pipeline**. You describe the intended fabric state once in NetBox; this repo:

1. Reads that intent (`generate.py` → NetBox REST API)
2. Validates it against a JSON Schema (catches typos before they reach a device)
3. Renders per-vendor configs via Jinja2 templates
4. Deploys them transactionally via NAPALM (Arista cEOS) or `frr-reload.py` over SSH (FRR)
5. Detects drift between intent and running state (`compliance.py`, `controller.py`)
6. Emits structured events to the observability stack (`obs_sink.py`)

The same SoT can generate the configs for `clab-fabric-evpn`'s full 4-node fabric (see `tests/test_full_fabric_render.py`). This is the integration claim: **the automation can actually deploy the fabric**.

---

## 2. Mental model — the GitOps loop

```
       ┌──────────────────┐
       │ NetBox  (SoT)    │  ← Engineer or NetBox-API automation edits here
       │ devices, peers,  │
       │ interfaces, VNIs │
       └────────┬─────────┘
                │ REST API (pynetbox / requests)
                ▼
       ┌──────────────────┐
       │ generate.py      │  ← Pulls SoT, validates against JSON Schema,
       │                  │     renders per-vendor templates → output/
       └────────┬─────────┘
                │ output/{device}.conf
                ▼
       ┌──────────────────┐
       │ Git (MR / review) │  ← Optional human gate; CI runs idempotency tests
       └────────┬─────────┘
                │ merged
                ▼
       ┌──────────────────────────────────────────────────────┐
       │ deploy.py                                            │
       │   - filesystem lock (one deploy per host at a time)  │
       │   - --max-delta diff guard                           │
       │   - --canary + Prometheus convergence gate           │
       │   - NAPALM transactional commit (EOS)                │
       │   - frr-reload.py via SFTP (FRR)                     │
       │   - emit deploy events to obs_sink                   │
       └────────┬─────────────────────────────────────────────┘
                │ applied to live devices
                ▼
       ┌──────────────────┐
       │ Devices          │
       │ (cEOS / FRR)     │
       └────────┬─────────┘
                │ RANCID polls, dumps running config to Git
                │ controller.py polls device + diffs vs intent
                ▼
       ┌──────────────────┐
       │ compliance.py    │  ← Diff running-vs-intent, classify severity,
       │ controller.py    │     route major drift to HITL approval queue
       └──────────────────┘
                │
                │ events via obs_sink → obs-telemetry's ES
                ▼
       (telemetry stack picks it up)
```

The loop is closed: changes flow out, RANCID brings running state back, controller detects divergence, HITL routes corrections. This is the difference between "we have automation" and "we have a control loop."

---

## 3. Tools used — what they are and why

### 3.1 NetBox

**What it is:** Open-source IPAM + DCIM (IP Address Management + Data Center Infrastructure Management). It models devices, racks, sites, interfaces, IP addresses, VLANs, BGP sessions, and so on. REST API on `:8000`; web UI for humans.

**Why it's the SoT (Source of Truth):** Operators trust it. It's the de facto inventory tool in network ops. It exposes a typed schema via the REST API, which means we can pull authoritative state programmatically.

**How we use it:** `generate.py` calls `GET /api/dcim/devices/?limit=0`, gets a list of device JSON objects, and renders templates from them. For lab/CI purposes, if NetBox isn't reachable, we fall back to fixture JSON files in `tests/fixtures/`.

**Where it lives:** Docker-Compose stack in `netbox/docker-compose.yml`. Bring up with `make netbox-up`.

**Auth:** Token-based. `NETBOX_TOKEN` env var. `generate.py` fails loudly if it isn't set (no silent fallback to a placeholder token).

### 3.2 JSON Schema (`schemas/device.schema.json`)

**What it is:** Draft 2020-12 JSON Schema describing the exact shape of a "device" in our SoT — required fields, allowed values, format constraints.

**Why we use it:** NetBox is flexible (custom fields, plugins). Without a schema, garbage in → garbage out → bad config on a device. The schema catches at *render time* what would otherwise be caught at *deploy time* (or worse, runtime).

**What it validates:**
- `name` matches `^[a-z][a-z0-9-]{1,62}$` (lowercase, RFC-style hostnames)
- `platform` is one of `frr|arista_eos|juniper_junos|cisco_iosxr`
- `asn` is in `[1, 4294967295]` (valid BGP ASN range)
- `router_id` is a valid IPv4
- `peers` is a non-empty array with each peer having `ip`, `asn`, `description`
- Optional blocks (`interfaces`, `bfd_profile`, `graceful_restart`, `evpn`, `policies`) have their own nested schemas

**Why JSON Schema and not Pydantic:** See [ADR 0003](adr/0003-json-schema-not-pydantic.md). Cross-language contract: same schema validates NetBox UI input, CI rendering, and downstream tooling regardless of language.

### 3.3 Jinja2

**What it is:** Python templating engine. Templates have `{{ var }}` interpolation and `{% for/if %}` control flow. The Python code calls `template.render(device=device)` and gets back the rendered string.

**Why we use it:** Configs have repetitive structure that varies by data (different ASNs, peer IPs, etc.). Jinja2 lets us write the structure once and substitute the data per device.

**Where the templates live:**
- `templates/frr/bgp_peer.j2` — full-featured (EVPN, BFD, policies, GR)
- `templates/arista/bgp_peer.j2` — full-featured for cEOS syntax
- `templates/juniper/bgp_peer.j2` — basic BGP (reference)
- `templates/cisco/bgp_peer.j2` — basic BGP (reference)

See `templates/README.md` for the feature-coverage matrix per vendor.

**Strict mode:** We use `Environment(undefined=StrictUndefined)`. If a template references `device.nonexistent_field`, Jinja2 raises immediately instead of silently rendering blank. Fail loud.

### 3.4 NAPALM

**What it is:** "Network Automation and Programmability Abstraction Layer with Multivendor support." A Python library that abstracts vendor-specific config protocols (eAPI for Arista, NETCONF for Junos, NX-API for Cisco NX-OS) behind a uniform interface:

```python
from napalm import get_network_driver
driver = get_network_driver("eos")
device = driver(hostname=..., username=..., password=...)
device.open()
device.load_merge_candidate(config=new_config)
diff = device.compare_config()
device.commit_config()  # atomic, transactional
device.close()
```

**Why we use it for Arista cEOS:** The `eos` driver wraps `pyeapi` and gives us atomic commit semantics — `load_merge_candidate` stages the config without applying, `compare_config` shows what *would* change, `commit_config` applies in one atomic vtysh transaction. If any line fails parse, nothing is applied.

**This is the gold standard for vendor automation:** stage → diff → commit/discard. NAPALM provides this for EOS, NX-OS, JunOS, IOS-XR.

### 3.5 Netmiko

**What it is:** A lower-level Python library for SSH automation. Connects via SSH, sends raw commands, parses prompts. Used when NAPALM doesn't have a driver, or when you need to run arbitrary shell commands on a Linux-based NOS.

**Why we use it for FRR:** NAPALM has no first-class FRR driver. We use Netmiko's SSH transport but **wrap `frr-reload.py`** instead of sending config line-by-line (see Section 3.6).

### 3.6 `frr-reload.py` (FRR's official transactional reloader)

**What it is:** A Python script shipped with FRR (`/usr/lib/frr/frr-reload.py`) that:
1. Reads the current running config
2. Compares to a candidate config file
3. Computes the *minimal* set of vtysh commands to converge running → candidate
4. Applies them in a single vtysh transaction
5. Rolls back automatically if any command fails parse

**Why we use it instead of line-push:** Pushing 200 lines of `frr.conf` via `Netmiko.send_config_set` is non-atomic. Line 47 might fail and lines 1-46 are already applied — device is now in a half-state. `frr-reload.py` is **transactional** like NAPALM's `load_merge_candidate` / `commit_config`.

**How we invoke it (real implementation):**
```python
# deploy.py — deploy_with_frr_reload()
import paramiko
client = paramiko.SSHClient()
client.connect(hostname=..., username=..., password=...)
sftp = client.open_sftp()
sftp.put(local_config_path, "/tmp/frr-leaf2-new.conf")  # stage via SFTP
sftp.close()

# Phase 1: validate (parse-only)
_, test_out, _ = client.exec_command("sudo frr-reload.py --test --reload /tmp/frr-leaf2-new.conf")

# Phase 2: atomic apply
_, apply_out, _ = client.exec_command("sudo frr-reload.py --reload /tmp/frr-leaf2-new.conf")
```

See [ADR 0004: frr-reload, not Netmiko line-push](adr/0004-frr-reload-not-netmiko-lines.md) for why this matters.

### 3.7 SaltStack

**What it is:** A configuration management system (like Ansible/Puppet/Chef) with a master-minion architecture and Python-based state files (`.sls`).

**Why it's here:** Demonstrates an alternative deploy path. The `salt/states/bgp_peers.sls` state declares "this file should be at /etc/frr/frr.conf with these contents." A Salt minion on the device pulls and applies it.

**Current wiring:** Salt states exist as reference (`salt/top.sls`, `salt/states/bgp_peers.sls`) but the active deploy path is NAPALM+Netmiko via `deploy.py`. The Salt portion shows how the same SoT-rendered configs could feed a declarative pull-based orchestrator.

### 3.8 RANCID

**What it is:** "Really Awesome New Cisco confIg Differ" — an old, robust tool that periodically SSHes into network devices, dumps their running config, and commits any changes to Git. The Git history *is* your audit trail.

**Why it's in this pipeline:** RANCID closes the loop. After `deploy.py` applies a change, RANCID's next polling cycle (typically hourly) dumps the running config and commits the diff. If running diverges from intent, you see it in two places:
- RANCID's git diff
- `compliance.py` running its own diff against intent

**Config:** `rancid/rancid.conf` declares the device targets and their auth types. In a real deployment, RANCID's collected configs live in `/var/lib/rancid/<group>/configs/<device>` as plain text, version-controlled.

### 3.9 pyca/paramiko

**What it is:** Pure-Python SSH library. Lower-level than Netmiko (no prompt parsing). Used for SFTP file staging and bare `exec_command` invocation.

**Why we use it directly:** Netmiko is overkill for "scp a file then run one command." Paramiko's SFTP transport is cleaner.

### 3.10 obs_sink (shared helper)

**What it is:** A 30-line Python module that emits structured events to both:
- A local JSONL file (default — durable, decoupled from any service)
- An ES endpoint (when `OBS_SINK_ES_URL` is set)

**Why it's shared with clab-ai-mcp:** Both repos emit events (`deploy.py`, `compliance.py`, `controller.py` here; `agent.py`, `mcp_server.py` there). They write the same shape so the downstream telemetry stack ingests one stream. See `obs_sink.py` — 50 lines, no dependencies.

### 3.11 pytest + jsonschema

**What it is:** pytest is the Python test framework. `jsonschema` is the JSON Schema validator.

**Why we use them:** Tests run on every CI push:
- `test_idempotent.py` — rendering twice produces byte-identical output
- `test_schema.py` — fixtures pass, malformed inputs are rejected
- `test_multivendor.py` — every vendor template renders for its respective fixture
- `test_fabric_render.py` — single-leaf full-feature render
- `test_full_fabric_render.py` — every node of the fabric (spine1/spine2/leaf1/leaf2) renders with EVPN/BFD/policies; community-attach + unnumbered fixtures also covered

---

## 4. Repository structure — every directory + file

```
clab-automation/
├── .github/workflows/
│   └── ci.yml                     # GitHub Actions: lint + pytest
├── compliance.py                  # Drift detection: pull running, diff vs intent
├── controller.py                  # Pull-based reconciliation loop (long-lived)
├── deploy.py                      # Render output → apply to devices
├── docs/
│   ├── DESIGN.md                  # THIS FILE
│   ├── THREAT_MODEL.md            # STRIDE-style threat analysis
│   └── adr/
│       ├── 0001-napalm-plus-netmiko.md
│       ├── 0002-netbox-as-sot.md
│       ├── 0003-json-schema-not-pydantic.md
│       ├── 0004-frr-reload-not-netmiko-lines.md
│       └── 0005-reconciliation-controller.md
├── generate.py                    # NetBox / fallback → Jinja2 → output/
├── netbox/
│   ├── docker-compose.yml         # Local NetBox + PostgreSQL
│   └── seed.py                    # Populate NetBox with sample devices
├── obs_sink.py                    # Shared event emitter (JSONL + optional ES)
├── pyproject.toml                 # Python dependencies
├── rancid/
│   └── rancid.conf                # RANCID device target list
├── salt/
│   ├── states/bgp_peers.sls       # Declarative state (file.managed + service.running)
│   └── top.sls                    # Salt top file
├── schemas/
│   └── device.schema.json         # JSON Schema for device SoT objects
├── templates/
│   ├── README.md                  # Per-vendor coverage matrix
│   ├── arista/bgp_peer.j2         # Full-feature Arista EOS template
│   ├── cisco/bgp_peer.j2          # Basic Cisco IOS-XR reference
│   ├── frr/bgp_peer.j2            # Full-feature FRR template
│   └── juniper/bgp_peer.j2        # Basic Junos reference
├── tests/
│   ├── __init__.py
│   ├── fixtures/
│   │   ├── devices.json                       # 3-device basic fabric (FRR + cEOS)
│   │   ├── fabric_evpn_spine1.json            # Full clab-fabric-evpn spine1
│   │   ├── fabric_evpn_spine2.json            # ...spine2
│   │   ├── fabric_evpn_leaf1.json             # ...leaf1
│   │   ├── fabric_evpn_leaf2.json             # ...leaf2
│   │   ├── fabric_evpn_leaf1_unnumbered.json  # Unnumbered variant
│   │   ├── leaf_with_community.json           # Exercises set_large_community
│   │   └── multivendor_devices.json           # Cisco IOS-XR + Junos fixtures
│   ├── test_fabric_render.py      # Single-leaf full-feature assertions
│   ├── test_full_fabric_render.py # 4-node + variants
│   ├── test_idempotent.py         # Render twice, assert byte-identical
│   ├── test_multivendor.py        # Cisco + Junos templates render
│   └── test_schema.py             # JSON Schema validation tests
├── topology/
│   └── lab.clab.yml               # Multi-vendor topology (cEOS + FRR)
├── validate.py                    # Post-deploy BGP state check
├── .env.example
├── .gitattributes                 # Suppress GitHub Linguist detection
├── .gitignore
├── .gitlab-ci.yml                 # Legacy GitLab CI
├── .pre-commit-config.yaml
├── LICENSE                        # MIT
├── Makefile                       # netbox-up / generate / diff / deploy / validate / compliance
├── README.md
└── SECURITY.md
```

### 4.1 What each major file does

| File | Role |
|------|------|
| `generate.py` | Render configs. Reads SoT (NetBox or fallback fixture), validates each device against `schemas/device.schema.json`, renders the per-vendor template, writes `output/<name>.conf` |
| `deploy.py` | Apply configs. Filesystem lock, diff-bounds check, optional canary with Prometheus convergence gate, NAPALM (cEOS) or frr-reload (FRR), emit deploy events |
| `compliance.py` | Detect drift. Pull running config (RANCID dump or live SSH), normalize, diff against intent, emit drift records |
| `controller.py` | Continuous reconciliation. Long-lived process that runs `compliance.py` logic on every device in a loop. Classifies severity, routes major drift to HITL approval |
| `validate.py` | Post-deploy state check. SSHes into each device, runs `show bgp summary`, asserts `Established` |
| `obs_sink.py` | Single shared event sink. All three of deploy/compliance/controller emit via `obs_sink.emit()` |

---

## 5. Walking through each script

### 5.1 `generate.py` — SoT → rendered config

```python
NETBOX_URL = os.getenv('NETBOX_URL', 'http://localhost:8000')
NETBOX_TOKEN = os.getenv('NETBOX_TOKEN')
if not NETBOX_TOKEN:
    print("Warning: NETBOX_TOKEN not set — falling back to sample data")
```
Read env vars. If no token, fall back to fixtures (so tests / CI work offline).

```python
def get_devices_from_netbox():
    headers = {'Authorization': f'Token {NETBOX_TOKEN}'}
    url = f'{NETBOX_URL}/api/dcim/devices/?limit=0'
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get('results', [])
    except Exception as e:
        print(f"Error fetching from Netbox: {e}")
        return []
```
Hit NetBox REST API. `limit=0` = "all results, no pagination." Returns a list of device dicts.

```python
def validate_devices(devices):
    if not HAS_JSONSCHEMA:
        print("Warning: jsonschema not installed — skipping SoT validation")
        return devices
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for d in devices:
        for err in validator.iter_errors(d):
            errors.append(f"{d.get('name','<unnamed>')}: {err.message}")
    if errors:
        print("SoT validation failed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    return devices
```
**The schema guard.** If `jsonschema` is installed, validate every device. Fail loud (exit 1) on any violation, with the specific error for each bad field. This catches NetBox typos before they touch a template.

```python
def generate_configs(device_name=None):
    devices = get_devices_from_netbox()
    devices = validate_devices(devices)

    if not devices:
        # Fallback to sample data
        devices = [...]  # see file

    template_map = {
        'arista_eos': 'arista/bgp_peer.j2',
        'frr': 'frr/bgp_peer.j2',
        'juniper_junos': 'juniper/bgp_peer.j2',
        'cisco_iosxr': 'cisco/bgp_peer.j2',
    }

    Path('output').mkdir(exist_ok=True)
    for device in devices:
        if device_name and device.get('name') != device_name:
            continue
        platform = device.get('platform', 'frr')
        template_file = template_map.get(platform, 'frr/bgp_peer.j2')
        config = render_config(device, template_file)
        with open(f"output/{device.get('name')}.conf", 'w') as f:
            f.write(config)
        print(f"✅ Generated: output/{device.get('name')}.conf")
```
Map each device's platform to a template, render, write to `output/`. Output is gitignored (intermediate artifact).

### 5.2 `deploy.py` — the heart of the deploy logic

#### 5.2.1 Setup

```python
DEVICES = {
    'spine1': {'host': '127.0.0.1', 'driver': 'eos', 'port': 8001},
    'leaf1':  {'host': '127.0.0.1', 'driver': 'eos', 'port': 8002},
    'leaf2':  {'host': '127.0.0.1', 'driver': 'frr', 'port': 22},
}

DEVICE_USER = os.getenv("DEVICE_USER", "admin")
DEVICE_PASSWORD = os.getenv("DEVICE_PASSWORD")
```
Static device inventory. In production this would also come from NetBox; for the lab it's hardcoded. `DEVICE_PASSWORD` is read from env — no defaults, fails if absent.

#### 5.2.2 Filesystem lock

```python
def acquire_lock(path: Path):
    fh = path.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"❌ Another deploy is running (lock held: {path})")
        sys.exit(1)
    return fh
```
`fcntl.flock` with `LOCK_EX | LOCK_NB`: exclusive lock, non-blocking. If another `deploy.py` holds it, this exits with code 1 instead of waiting. The lock is implicit-release: when the process ends, the kernel releases it.

This prevents two engineers from racing `deploy.py` against each other.

#### 5.2.3 Diff-bounds guard (`--max-delta`)

```python
def check_diff_bounds(device_name, max_delta):
    new_path = Path(f"output/{device_name}.conf")
    if not new_path.exists():
        return True, 0
    try:
        prior = subprocess.check_output(
            ["git", "show", f"HEAD:output/{device_name}.conf"],
            stderr=subprocess.DEVNULL, text=True,
        )
    except subprocess.CalledProcessError:
        return True, sum(1 for _ in new_path.read_text().splitlines())

    new_lines = new_path.read_text().splitlines()
    prior_lines = prior.splitlines()
    diff = list(difflib.unified_diff(prior_lines, new_lines, lineterm=""))
    delta = sum(1 for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---")))
    return delta <= max_delta, delta
```
Compute how many lines changed between the current `output/<device>.conf` and the version in git HEAD. If the delta exceeds `--max-delta` (default 200), **refuse to deploy**. This catches the catastrophic case where a template bug renders a wildly different config; the safety net stops it before it ships.

#### 5.2.4 NAPALM deploy path (cEOS)

```python
def deploy_with_napalm(device_name, config_file):
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
    else:
        device.discard_config()
        outcome = "discarded"

    device.close()
    return {...}
```

Classic NAPALM pattern:
1. `load_merge_candidate` — stage the new config; nothing applied yet
2. `compare_config` — generate diff against running
3. If empty diff → idempotent no-op
4. Otherwise prompt operator (or auto-confirm if `--yes`) → `commit_config` or `discard_config`
5. Return a structured result for the event sink

#### 5.2.5 FRR deploy path (paramiko + frr-reload)

```python
def deploy_with_frr_reload(device_name, config_file):
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname="127.0.0.1", port=2222,
                   username=DEVICE_USER, password=DEVICE_PASSWORD, timeout=10)

    remote_path = f"/tmp/frr-{device_name}-new.conf"

    # SFTP file staging
    sftp = client.open_sftp()
    sftp.put(config_file, remote_path)
    sftp.close()

    def _run(cmd, timeout=30):
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        return stdout.channel.recv_exit_status(), stdout.read().decode(), stderr.read().decode()

    # Phase 1: parse-only test
    rc, test_out, test_err = _run(f"sudo frr-reload.py --test --reload {remote_path}")
    combined = test_out + test_err

    if rc != 0:
        # frr-reload couldn't parse the config
        return {"device": device_name, "outcome": "test-failed", ...}

    if "lines to add" not in combined and "lines to delete" not in combined:
        return {"device": device_name, "outcome": "no-op", "diff_lines": 0}

    if not (AUTO_YES or input(...) == "y"):
        return {"device": device_name, "outcome": "skipped", ...}

    # Phase 2: atomic apply
    rc, apply_out, apply_err = _run(f"sudo frr-reload.py --reload {remote_path}", timeout=60)
    outcome = "deployed" if rc == 0 else "apply-error"

    return {"device": device_name, "outcome": outcome, ...}
```

Three-phase pattern: **stage → test → apply**. The test phase is the safety net — if `--test` fails (syntax error, ref to undefined object), nothing reaches running config.

#### 5.2.6 Cross-system canary gate

```python
def wait_for_convergence(device, timeout_s, poll_s=5):
    started = time.time()
    while time.time() - started < timeout_s:
        established = prom_query(
            f'sum(frr_bgp_peer_state{{state="Established",instance="{device}"}})'
        )
        bfd_flaps = prom_query(
            f'sum(changes(frr_bfd_peer_uptime_seconds{{instance="{device}"}}[30s]))'
        )
        if established is None or bfd_flaps is None:
            print(f"[convergence-gate] {device}: telemetry not visible yet")
        elif established >= 1 and bfd_flaps == 0:
            return True, f"Established={int(established)} BFD_flaps=0"
        time.sleep(poll_s)
    return False, f"timeout after {timeout_s}s"
```

After the canary device deploys, **query Prometheus** for two signals:
- `frr_bgp_peer_state{state="Established"}` ≥ 1 (peers are up)
- `frr_bfd_peer_uptime_seconds[30s] changes == 0` (no BFD flap in last 30s)

Only when both hold for `convergence_timeout` seconds, proceed to the rest of the fleet. If timeout, abort the rollout.

This is **closed-loop deployment**: the deploy decision is gated on telemetry signal, not a fixed sleep.

#### 5.2.7 Safety budget

```python
if failures / len(devices) > args.safety_threshold:
    print(f"\n❌ Aborting: {failures}/{len(devices)} failures exceed safety budget")
    emit_event({"event": "run-aborted", "failures": failures, "total": len(devices)})
    sys.exit(2)
```

If more than `--safety-threshold` (default 1/3) of devices fail their deploy, abort the run. Blast-radius cap.

### 5.3 `compliance.py` — drift detection

```python
def normalize(config: str) -> list[str]:
    out = []
    for line in config.splitlines():
        stripped = line.rstrip()
        if _NOISE_RE.match(stripped):  # comments, banners, timestamps
            continue
        out.append(re.sub(r"\s+", " ", stripped).lower())
    return out
```
Normalize both the intended config and the actual config before diffing. Strip noise (comments starting with `!`, vendor banners, dynamic timestamps, route counts) so we only diff *semantic* content.

```python
def diff_device(device_name, max_drift_lines=20):
    intended = normalize(get_intended_config(device_name))
    actual = normalize(fetch_running_config(device_name))
    diff = list(difflib.unified_diff(actual, intended, ...))
    drift_lines = [d for d in diff if d.startswith(("+", "-")) and not d.startswith(("+++", "---"))]
    return {
        "device": device_name,
        "drift_line_count": len(drift_lines),
        "compliant": len(drift_lines) == 0,
        "over_threshold": len(drift_lines) > max_drift_lines,
        "diff": "\n".join(diff[:500]),
    }
```
`difflib.unified_diff` (stdlib, no external deps). Count the `+` and `-` lines as the drift size. Cap the embedded diff at 500 lines to keep the structured record bounded.

### 5.4 `controller.py` — pull-based reconciliation loop

```python
SEVERITY_MINOR_MAX_LINES = 5
SEVERITY_MAJOR_THRESHOLD = 50

def classify(drift_record):
    n = drift_record["drift_line_count"]
    if n == 0:                              return "converged"
    if n <= SEVERITY_MINOR_MAX_LINES:       return "minor"
    if n <= SEVERITY_MAJOR_THRESHOLD:       return "moderate"
    return "major"

def reconcile_once(devices, max_drift_lines):
    summary = {"converged": 0, "minor": 0, "moderate": 0, "major": 0}
    for device in devices:
        record = diff_device(device, max_drift_lines)
        emit_drift_record(record)
        sev = classify(record)
        summary[sev] += 1
        if sev == "major":
            approval_id = file_hitl_approval(device, record)
            event["action"] = "filed_approval"
            event["approval_id"] = approval_id
        emit_controller_event(event)
    return summary

# Main loop
while not _shutdown:
    reconcile_once(devices, args.max_drift_lines)
    time.sleep(args.interval)
```

The Kubernetes operator pattern applied to network config: continuous loop, every N seconds, classify drift, route major to HITL.

**Why "minor" drift isn't auto-remediated:** if the SoT is wrong, auto-remediation propagates the wrong thing fleet-wide. Detection without unilateral action. See [ADR 0005](adr/0005-reconciliation-controller.md).

---

## 6. The CI pipeline

GitHub Actions runs on every push:

```yaml
lint:
  - yamllint . --ignore-path .gitignore
  - ruff check .

test:
  - pip install jinja2 jsonschema pytest
  - pytest tests/ -v
```

Test categories:
- **`test_schema.py`** — fixtures pass schema; malformed inputs (missing field, out-of-range ASN, bad hostname) rejected
- **`test_idempotent.py`** — render twice, assert byte-identical
- **`test_multivendor.py`** — Cisco IOS-XR + Junos templates render with their fixtures
- **`test_fabric_render.py`** — single-leaf full feature assertions
- **`test_full_fabric_render.py`** — 4 nodes × full features × ASN-uniqueness × leaf-vs-spine attribute checks

If any test fails, the merge is blocked.

---

## 7. Operational walkthrough — deploying a real change

Scenario: add a new peer to `leaf1`.

### Step 1 — edit SoT in NetBox

UI or API: navigate to `leaf1` device → BGP Peers → Add → fill in `ip`, `asn`, `description`. Save.

### Step 2 — generate locally

```bash
$ source .env  # exports NETBOX_TOKEN, DEVICE_PASSWORD
$ python generate.py --all
✅ Generated: output/spine1.conf
✅ Generated: output/spine2.conf
✅ Generated: output/leaf1.conf
✅ Generated: output/leaf2.conf
```

### Step 3 — review diff

```bash
$ git status
modified: output/leaf1.conf

$ make diff
diff --git a/output/leaf1.conf b/output/leaf1.conf
@@ -52,6 +52,8 @@
   neighbor 10.10.1.1 description spine1
   neighbor 10.10.3.1 peer-group SPINES
   neighbor 10.10.3.1 description spine2
+  neighbor 10.10.99.1 peer-group SPINES
+  neighbor 10.10.99.1 description new-peer
```

### Step 4 — open PR

```bash
$ git checkout -b add-leaf1-peer
$ git add output/
$ git commit -m "Add new peer to leaf1"
$ git push origin add-leaf1-peer
```
CI runs: lint + tests + render verification. Reviewer approves.

### Step 5 — deploy with canary

```bash
$ python deploy.py --device leaf1 --max-delta 50 --canary leaf1 --convergence-timeout 120
[lock acquired]
[diff bounds: 8 lines, within limit]
[NAPALM merge] Loading candidate config to leaf1...
[NAPALM diff] 2 new neighbor stanzas + 1 description change
Deploy to leaf1? [y/N]: y
[NAPALM commit] ✅ leaf1: deployed
[canary gate] querying Prometheus...
[convergence-gate] leaf1: Established=2 BFD_flaps=0
✅ Canary converged (Established=2 BFD_flaps=0) — proceeding
```

`deploy-events.jsonl` now contains a record of this change. `obs-telemetry` ingests it; Grafana shows "Deploys per hour" tick up.

### Step 6 — RANCID dumps config

Within an hour, RANCID's polling cycle SSHes into leaf1, dumps `show running-config`, and commits the diff to its own git repo. Audit trail closes.

### Step 7 — `controller.py` notices no drift

Continuous reconciliation loop on its next iteration: pulls running, diffs against intent, drift = 0. No action needed. Emits `reconcile_check` event with severity=converged.

---

## 8. Failure modes — what can go wrong

| Failure | Symptom | Where to look |
|---------|---------|---------------|
| NetBox unreachable | `generate.py` falls back to fixtures with warning | Network connectivity to NetBox; check `NETBOX_URL` |
| Schema validation fails | `generate.py` exits 1 with specific field error | Fix the SoT in NetBox |
| Template render error | `generate.py` prints "Error rendering template..." | Check `templates/<vendor>/bgp_peer.j2` for missing field references (use StrictUndefined to catch early) |
| Diff bounds exceeded | `deploy.py` exits without applying | Investigate the template — likely a bug rendering too much |
| Filesystem lock held | `deploy.py` exits "Another deploy is running" | Wait or `rm .deploy.lock` if you know the prior process died |
| NAPALM commit fails (syntax) | `compare_config` shows partial state | `load_merge_candidate` parses *some* errors locally; `commit_config` may surface remaining issues; check device logs |
| frr-reload test fails | rc != 0 from `--test` phase | Read `test_err` output — usually a typo in the config |
| Canary doesn't converge | `wait_for_convergence` times out, exits with code 3 | Check Prometheus is scraping the device; investigate if BGP/BFD are actually broken |
| Safety threshold tripped | Mass failure on `--all` deploy | First failed devices' events in `deploy-events.jsonl` show what broke |
| Drift detection misses things | `compliance.py` says compliant but RANCID shows changes | Check `_NOISE_PATTERNS` — may be normalizing away real signal |

---

## 9. Threat model — who's the attacker

See [`docs/THREAT_MODEL.md`](THREAT_MODEL.md) for the STRIDE analysis. Highlights:

- **NetBox is the primary trust boundary.** Anything coming out of it could have been edited by the inventory team without engineering review → JSON Schema validation is the contract.
- **DEVICE_PASSWORD has the highest blast radius** — controls all fabric devices. Lives in env vars (lab); should be Vault dynamic secrets in production.
- **Compromised CI runner cannot deploy.** Deploy creds live only on the engineer's host. CI renders configs (artifacts), nothing more.
- **Known gaps:** no artifact signing between `generate.py` output and `deploy.py` input (local FS tampering possible); no `MAX_DEVICES` bound (DoS vector).

---

## 10. What this repo deliberately doesn't do

POC-vs-fleet trade-offs:

- **No fleet inventory at scale.** The 3-device DEVICES dict is hardcoded; real fleets pull from NetBox per-deploy
- **No artifact signing.** `output/` flows directly; could be tampered with locally
- **No GitOps webhook.** Deploys are manually triggered by engineers; production might fire on Git merge
- **Salt is reference only.** The active path is NAPALM+Netmiko via `deploy.py`
- **RANCID is reference.** Doc explains how it would fit; no live RANCID deployment
- **No multi-region orchestration.** Single host runs `controller.py`; production would have leader election + sharded responsibility

---

## 11. What to read next

1. [`schemas/device.schema.json`](../schemas/device.schema.json) — the SoT contract
2. [`templates/frr/bgp_peer.j2`](../templates/frr/bgp_peer.j2) — full-feature template (renders the EVPN fabric)
3. [`tests/test_full_fabric_render.py`](../tests/test_full_fabric_render.py) — proves auto-config can build the whole fabric
4. [`docs/adr/`](adr/) — every major design choice explained
5. **Sibling repos:**
   - [`clab-fabric-evpn`](https://github.com/pradeepbc86/clab-fabric-evpn) — what we deploy *to*
   - [`clab-observability`](https://github.com/pradeepbc86/clab-observability) — what consumes our `deploy-events.jsonl`
   - [`clab-ai-mcp`](https://github.com/pradeepbc86/clab-ai-mcp) — the LLM that can propose changes through this pipeline
