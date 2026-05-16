# Threat Model — clab-automation

STRIDE-style threat model for the multi-vendor BGP config GitOps pipeline.

## System overview

```
┌─────────────────┐         ┌─────────────────┐
│  Operator       │         │  NetBox UI      │
│  (engineer)     │         │  (other team)   │
└────────┬────────┘         └────────┬────────┘
         │                           │
         │ git push                  │ create/update
         ▼                           ▼
┌────────────────────────────────────────────┐
│  Git (GitHub)                              │
│  - generate.py / deploy.py / templates/    │
│  - .github/workflows/ci.yml                │
└────────┬───────────────────────────────────┘
         │ runner pulls
         ▼
┌────────────────────────────────────────────┐
│  CI runner (GitHub Actions)                │
│  - generate.py → output/                   │
│  - tests/ (idempotency, schema)            │
└────────┬───────────────────────────────────┘
         │ artifacts → manual deploy
         ▼
┌────────────────────────────────────────────┐
│  Deploy host (engineer's laptop / bastion) │
│  - reads .env (DEVICE_PASSWORD, NETBOX_TOKEN)│
│  - deploy.py → NAPALM (cEOS) / Netmiko (FRR)│
│  - emits deploy events → observability     │
└────────┬───────────────────────────────────┘
         │ HTTPS (eAPI) / SSH (vtysh)
         ▼
┌────────────────────────────────────────────┐
│  Fabric devices (cEOS + FRR)               │
└────────────────────────────────────────────┘
         ▲
         │ poll
┌────────────────────────────────────────────┐
│  RANCID                                    │
│  - SSH device, dump config, commit to git  │
└────────────────────────────────────────────┘
```

## Trust boundaries

| # | Boundary | Crossing | Trust transition |
|---|---------|----------|------------------|
| 1 | Engineer → Git | git push (SSH/HTTPS) | Authenticated (SSH key / token) |
| 2 | NetBox UI → NetBox DB | HTTPS | NetBox-internal authentication |
| 3 | NetBox API → generate.py | REST + token | Untrusted data — must validate against `schemas/device.schema.json` |
| 4 | CI runner → output artifacts | GitHub Actions runtime | Trusted within run, untrusted across runs |
| 5 | Deploy host → device | SSH + eAPI | Mutual auth via SSH keys / cert + device-side AAA |
| 6 | Device → RANCID | SSH | RANCID has read-only credentials |

The critical boundary is **#3** — anything coming out of NetBox could have been edited by the inventory team without engineering review. JSON Schema validation is the contract.

## Assets

| Asset | Sensitivity |
|-------|-------------|
| Device SSH/eAPI credentials (`DEVICE_PASSWORD`) | High — controls all fabric devices |
| NetBox API token | High — controls SoT data |
| Generated configs (`output/`) | Medium — leaks topology if exfiltrated |
| Git history of `output/` | Medium — historical configs ≈ current configs |
| RANCID-collected device configs | Medium |
| Deploy events stream (to observability) | Low — operational metadata |

## STRIDE analysis

### S — Spoofing

| Threat | Mitigation |
|--------|------------|
| Attacker pushes a malicious config to Git | Branch protection + required reviewers + CI must pass |
| Spoofed NetBox API response (MITM) | TLS required; in lab we use HTTP for `localhost:8000` — **document HTTPS+cert for any real deployment** |
| Attacker impersonates RANCID to dump configs | RANCID uses dedicated read-only credentials, separate from deploy creds |
| Attacker spoofs a device to receive `DEVICE_PASSWORD` | SSH host-key checking + per-device known_hosts entries (TOFU acceptable in lab, strict in real fleet) |

### T — Tampering

| Threat | Mitigation |
|--------|------------|
| NetBox returns malformed/malicious device data | `validate_devices()` against JSON Schema before render; `StrictUndefined` in Jinja so missing fields fail loud |
| Attacker modifies `templates/` to inject backdoor config | All template changes go through PR review; CI renders fixtures and asserts they pass `idempotent` + `schema` tests |
| Attacker modifies `output/<device>.conf` between generate and deploy | Deploy reads from `output/` on disk — vulnerable to local FS tampering. **Mitigation:** sign artifacts (SHA-256 manifest in CI), verify before deploy. Currently a gap. |
| Attacker modifies live config out-of-band | Reconciliation loop (`compliance.py`) detects drift vs SoT |

### R — Repudiation

| Threat | Mitigation |
|--------|------------|
| Engineer denies pushing a change | Git history with signed commits (`git commit -S`) — operator-discipline gap |
| Engineer denies running deploy | Deploy events emitted to observability's Elasticsearch with username + diff size + timestamp |
| Device denies receiving change | RANCID-collected config snapshots in git serve as evidence |

### I — Information disclosure

| Threat | Mitigation |
|--------|------------|
| `.env` accidentally committed | `.env` in `.gitignore`; `detect-secrets` pre-commit hook scans for credentials |
| NETBOX_TOKEN logged in error messages | `generate.py` only logs the URL and status code, never the token |
| `output/` committed to git | `output/` in `.gitignore` |
| Device config exposed via RANCID git repo | RANCID git repo treated as sensitive; access-controlled |

### D — Denial of service

| Threat | Mitigation |
|--------|------------|
| Attacker creates 10,000 devices in NetBox to stall generate | `generate.py` has no explicit limit. **Gap:** add a `MAX_DEVICES` env-var bound. |
| Attacker pushes a config that crashes the device parser | NAPALM `load_merge_candidate` parses before commit; bad syntax surfaces in CI render |
| Attacker triggers a deploy storm | Deploy locking (one deploy at a time per host) + canary sequencing in `deploy.py` |
| Attacker fills `output/` with garbage | `output/` regenerated each run; bounded by NetBox device count |

### E — Elevation of privilege

| Threat | Mitigation |
|--------|------------|
| Compromised CI runner gains DEVICE_PASSWORD | DEVICE_PASSWORD only present on deploy host, **never in CI**. CI generates configs only. |
| Compromised template adds `enable secret` or shell escape | Template changes go through PR review; `tests/test_idempotent.py` re-renders and human reviews |
| Engineer escalates from read-only Netbox token to write | Token scope enforced by NetBox; SoT changes go through NetBox's own RBAC |

## Known gaps (deliberately accepted at lab scale)

1. **No artifact signing** between generate.py output and deploy.py input. Local FS tampering possible. Fix: SHA-256 manifest signed with a release key.
2. **No MAX_DEVICES bound in generate.py** — DoS vector.
3. **Deploy reads creds from `.env`** — engineer must remember to wipe. Production: Vault dynamic secrets per-deploy.
4. **No canary backoff** — if device 1 of 10 fails, deploy.py continues. Fix: `--safety-threshold` flag (planned).
5. **RANCID credentials separate from deploy** but both stored in `.env`. Production: separate accounts in separate stores.

## Out of scope

- Physical security of devices
- Network-layer ACLs preventing unauthorized SSH from non-deploy hosts
- Insider threat from operators with legitimate access
- Supply-chain attacks on `napalm` / `netmiko` / `jinja2` packages — covered by pinning + `pip-audit` in CI
