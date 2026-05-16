# ADR 0004 — `frr-reload.py` for FRR deploys, not Netmiko line-push

**Status:** Accepted
**Date:** 2026-05-16
**Supersedes:** parts of ADR 0001 (Netmiko remains used, but only as the SSH transport for frr-reload)

## Context

The original deploy path for FRR devices used Netmiko's `send_config_set(config.split("\n"))` — push each line of the rendered `frr.conf` into the running vtysh session. This is the path of least resistance for any SSH-only device, but it has three serious operational problems:

1. **No atomicity.** If line 47 has a syntax error, lines 1-46 are already applied. The device is now in a half-state — neither old nor new. There is no automatic rollback.
2. **No idempotency check.** Pushing the same config twice runs all commands twice. FRR will warn but still execute the no-ops, polluting logs and risking transient timing bugs.
3. **No diff.** We can't tell the operator "here's the 12 lines that will change." We can only tell them "here are 200 lines, hope it works."

Combining this with our portfolio claim of "GitOps drift detection" was a stretch — drift detection without atomic apply is detection-only, not reconciliation.

## Decision

Deploy FRR configs via `frr-reload.py`, FRR's official differential config tool. The Netmiko SSH transport stays — we just use it to invoke `frr-reload.py` rather than feeding lines to `vtysh -c`.

```python
# Stage
scp frr.conf device:/tmp/frr-new.conf

# Validate (parse-only)
frr-reload.py --test --reload /tmp/frr-new.conf

# Apply (atomic at vtysh layer)
frr-reload.py --reload /tmp/frr-new.conf
```

## Rationale

`frr-reload.py` is a Python tool shipped with FRR (`/usr/lib/frr/frr-reload.py`) that:

- Reads the candidate config from a file
- Compares it to the current running config
- Computes the minimal `no X` + `X` command set to converge
- Submits the diff to vtysh in a single transaction
- Validates syntactically before applying (`--test` mode)
- Rolls back automatically on any vtysh parse error

This is the same primitive NVIDIA Cumulus and SONiC ship for declarative FRR management. It's what `ifreload`, `netplan`, and `nmcli` do for their respective domains — convert declarative intent into the minimal imperative steps.

## Trade-offs accepted

- **Dependency on `frr-reload.py`** existing at `/usr/lib/frr/frr-reload.py`. It's bundled with FRR ≥ 7.4 and is in the upstream Docker image we use.
- **Heredoc file staging via Netmiko** is hacky. Real deployments would use `scp` or a sidecar HTTP fetch — easy to swap once we have a proper deploy host.
- **Sudo required** — `frr-reload.py` needs to write into FRR's running config. Device user must have `NOPASSWD` for this specific command. Trade-off vs giving the deploy user blanket sudo.

## What this unlocks

- Honest "drift detection" claim — running config can now be reliably reconciled to intent
- Test-mode validation in CI: render template → push to a throwaway FRR container → `frr-reload.py --test` — pre-merge sanity
- Per-device transactional rollback in deploy.py — already present for NAPALM/cEOS, now also true for FRR

## What this doesn't fix

- Cross-device atomicity: applying changes to 3 devices is still 3 independent transactions. If device 2 fails after device 1 succeeds, the fleet is in a mixed state. The reconciliation controller (see `controller.py`) handles this at a different layer — it keeps re-converging until the whole fleet matches intent.

## See also

- [FRR documentation: frr-reload](https://docs.frrouting.org/projects/dev-guide/en/latest/topotests.html#frr-reload-py)
- ADR 0001 (NAPALM+Netmiko rationale — Netmiko still used as transport, just not for line-push)
- `controller.py` — fleet-level convergence loop on top of device-level transactional applies
