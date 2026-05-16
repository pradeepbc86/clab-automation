# ADR 0001 — NAPALM for cEOS, Netmiko for FRR

**Status:** Accepted
**Date:** 2026-05-16

## Context

We need to deploy generated configs to two device types: Arista cEOS and FRRouting. Multiple Python libraries compete: NAPALM, Netmiko, Nornir, Scrapli, ncclient, pyeapi.

## Decision

- **cEOS → NAPALM** (`eos` driver, which wraps pyeapi)
- **FRR → Netmiko** (raw SSH + `vtysh` heredoc)

## Rationale

NAPALM's value is **vendor-neutral candidate-config + diff + commit/rollback**. The `eos` driver gives us:

- `load_merge_candidate(config=...)` — atomic stage
- `compare_config()` — diff before commit
- `commit_config()` / `discard_config()` — explicit promotion or abort
- `rollback()` — built-in safety net via running-config snapshot

FRR has none of these primitives natively. NAPALM does not have a first-class FRR driver (community `napalm-frr` exists but is unmaintained and shells out to `vtysh` anyway). Netmiko gives us direct SSH into the container, where we `vtysh -c "configure terminal"`-style apply lines — basic but functional. We accept that FRR deploys are non-transactional in this lab.

## Trade-offs accepted

- Two code paths (`deploy_with_napalm`, `deploy_with_netmiko`) instead of one — but they're each ~20 lines and the function-pointer dispatch is clear.
- No automatic rollback on FRR — operator must manually re-deploy the prior `output/leaf2.conf` if a deploy goes wrong. In a real fleet you'd version-control the output and use the previous git SHA as the rollback target.
- NAPALM brings a heavy dependency tree (paramiko, netaddr, ncclient). Acceptable for an automation host; not for embedded.

## Alternatives considered

- **Scrapli** — faster than Netmiko (asyncio), cleaner API, but ecosystem is younger and AAA integration is thinner.
- **Nornir** as the orchestrator on top of both — better for fleet scale (parallel inventory + per-device retries) but adds a layer of abstraction for a 3-device lab.
- **Ansible** — well-known, but harder to test in CI without a managed inventory; we'd lose the type-checked Python contract.

## See also

- [NAPALM EOS driver](https://napalm.readthedocs.io/en/latest/support/eos.html)
- [Netmiko supported devices](https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md)
