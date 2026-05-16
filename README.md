# clab-automation

Multi-vendor BGP config GitOps pipeline: Netbox as Source of Truth, Jinja2 templates, NAPALM + Netmiko deployment, SaltStack orchestration, RANCID for config backup and drift detection.

## Pipeline

```
Netbox (SoT)
  ↓
generate.py (Jinja2 render per vendor)
  ↓
Git MR (code review)
  ↓
lint CI (yamllint + ruff)
  ↓
deploy.py — NAPALM (cEOS) + Netmiko (FRR)   ← manual stage
  ↓                                            (SaltStack states live alongside
validate.py (post-deploy BGP check)            in salt/ for declarative variant)
  ↓
RANCID (backup running config to Git)
  ↓
compliance.py (drift detection vs Netbox SoT)
```

## Topology (ContainerLab)

```
spine1 (Arista cEOS, AS 65000) ←→ leaf1 (Arista cEOS, AS 65001)
spine1                         ←→ leaf2 (FRRouting,   AS 65002)
leaf1                          ←→ leaf2
```

- **spine1, leaf1**: Arista cEOS (requires free registration, NAPALM `eos` driver)
- **leaf2**: FRRouting (public Docker Hub, Netmiko SSH)
- **SaltStack**: Orchestrates both via proxy minions

## Tools

| Tool | Purpose |
|------|---------|
| **ContainerLab** | Topology orchestration (cEOS + FRR nodes) |
| **Arista cEOS** | Containerized EOS (control + data plane) |
| **FRRouting** | Open-source routing (control + data plane) |
| **Jinja2** | Multi-vendor config templating |
| **NAPALM** | cEOS config deployment + diff + rollback |
| **Netmiko** | FRR SSH-based config deployment |
| **Netbox** | Network Source of Truth (REST API) |
| **SaltStack** | Config orchestration across devices |
| **RANCID** | Config backup + Git version control |
| **GitLab CI/CD** | Pipeline automation (lint, generate, deploy) |

## Vendor Templates

- `templates/frr/bgp_peer.j2` — FRRouting BGP peer config
- `templates/juniper/bgp_peer.j2` — Juniper JunOS (for reference)
- `templates/cisco/bgp_peer.j2` — Cisco IOS-XR (for reference)
- `templates/arista/bgp_peer.j2` — Arista EOS

## Quick Start

```bash
# 1. Copy env template and fill in values
cp .env.example .env
# Edit .env — set NETBOX_TOKEN, DEVICE_PASSWORD

# 2. Start Netbox
make netbox-up
# Then log in at http://localhost:8000 (admin/admin) and generate an API token
# Bootstrap device-types / sites / platforms via the Netbox UI before seeding

# 3. Source env and populate Netbox
set -a; source .env; set +a
python netbox/seed.py

# 4. Generate configs from Netbox + Jinja2 (falls back to sample data if Netbox empty)
python generate.py --all

# 5. View diffs
make diff

# 6. Deploy to lab devices (interactive; use --yes for CI)
python deploy.py --device spine1

# 7. Validate post-deploy
python validate.py

# 8. Detect drift vs SoT
python compliance.py
```

## Files

- `topology/lab.clab.yml` — ContainerLab topology
- `generate.py` — Netbox → Jinja2 → rendered configs
- `deploy.py` — NAPALM/Netmiko deployment with diff/rollback
- `validate.py` — BGP neighbor state check
- `compliance.py` — RANCID diff + RPKI validation
- `netbox/` — Docker Compose + seed script
- `salt/` — SaltStack states
- `rancid/` — RANCID config backup
- `templates/` — Jinja2 config templates
- `.gitlab-ci.yml` — Pipeline (lint → generate → diff → deploy)

## Prerequisites

- Docker (Netbox, RANCID)
- ContainerLab (https://containerlab.dev/)
- Python 3.9+ (napalm, netmiko, pynetbox, jinja2, salt)
- Arista cEOS image (free registration at arista.com)
- FRRouting Docker image: `frrouting/frr:9.1.0`
