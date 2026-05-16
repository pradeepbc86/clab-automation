# ADR 0005 — Pull-based reconciliation controller alongside push CI

**Status:** Accepted
**Date:** 2026-05-16

## Context

The portfolio's automation story was originally push-only:

1. SoT changes in NetBox
2. CI/CD pipeline renders configs (`generate.py`)
3. Operator runs `deploy.py` manually or via a CI job
4. `compliance.py` runs ad-hoc to detect drift

This is fine for human-driven changes. It is insufficient for the class of failures where running state silently diverges from intent without anyone making a change:

- Operator runs `vtysh ... no neighbor X` on a device during incident response and forgets to put it back
- Vendor "auto-tuning" feature rewrites a knob (BGP timer adjustment, MSS clamping)
- Kernel upgrade resets an interface MTU
- A bad config push from a sibling automation system leaves a residue

Push-based pipelines are blind to these. They only fire on explicit change.

## Decision

Run `controller.py` as a long-lived process that performs pull-based reconciliation on a configurable interval. The controller:

1. Reads SoT from NetBox (or fallback sample data) every N seconds
2. For each device: fetches running config via RANCID or `docker exec vtysh -c "show running-config"`
3. Re-renders intent via the same `generate.py` pipeline that CI uses
4. Diffs the two
5. Classifies drift severity (converged / minor / moderate / major)
6. Emits structured events to `controller-events.jsonl` for observability to ingest
7. For major drift, files an approval request in `approvals/<uuid>.json` (same primitive as `clab-ai-mcp/tools/hitl.py`)
8. Minor drift is logged but not auto-remediated yet — flag exists in the event for future automation

## Rationale

This is the Kubernetes controller pattern applied to network state: a continuous control loop that observes, computes desired vs actual, and acts.

It is not a replacement for `deploy.py`. The two are complementary:

| Tool | When | What |
|------|------|------|
| `deploy.py` | Human-initiated, one-shot | Apply a specific change deliberately, with canary + safety budget |
| `controller.py` | Always running | Detect unintended divergence and route through HITL |

Both write to the same audit / event streams so observability sees one unified view of changes happening to the fabric.

## Why we don't auto-remediate everything

A naïve controller that just blindly re-applies SoT on every iteration is dangerous: if SoT is wrong, the controller propagates the wrong thing fleet-wide. The HITL gate on major drift acknowledges that the controller is not the source of authority — the human reviewer is. The controller's job is detection and queuing, not unilateral remediation.

Minor drift (≤5 lines) is auto-remediable in principle. We tag it but don't auto-apply yet — that's a follow-up gated on having alerting + telemetry sufficient to catch when the controller itself goes off the rails.

## Trade-offs accepted

- Polling vs event-driven. A true Kubernetes-style operator would watch a stream of change events from devices (gNMI streaming telemetry, NETCONF notifications). We poll because the gear we model (FRR, cEOS) doesn't expose a coherent change stream out of the box.
- Per-device cost. Each reconcile iteration runs `vtysh -c "show running-config"` per device. At fabric scale this is ~50ms × N devices. Acceptable up to a few hundred devices on a single controller; beyond that, shard.
- State held only in JSONL — no Postgres / Etcd. Single-controller deployment loses queue state on crash. For real fleet deployment, queue + state goes to Redis/Etcd with leader election (see ADR 0003 in clab-ai-mcp on the same trade-off).

## Drift severity tiers

| Tier | Lines diff | Action |
|------|------------|--------|
| converged | 0 | no-op |
| minor | 1-5 | log; flag as auto-remediable; do not act |
| moderate | 6-50 | log; visible in dashboards; manual review |
| major | >50 | file HITL approval; await human decision |

These numbers are not folklore — they're calibrated to the expected size of legitimate small fixes (a community tag, a single neighbor add) vs the size of accidental wholesale wipes.

## What this unlocks

- Closed-loop platform: detection → emission → optional remediation → audit. No more "we noticed drift in compliance.py but nobody acted on it."
- Observability of the platform itself: `controller-events.jsonl` gets ingested by observability's ES → a `controller_iterations_total` Prometheus counter can be derived → SLO on the platform is converging.
- HITL across two pipelines: same approval primitive serves the AI agent (clab-ai-mcp) and the reconciliation controller (clab-automation). Operator sees one queue.

## See also

- `compliance.py` — the per-device diff primitive the controller reuses
- `clab-ai-mcp/tools/hitl.py` — companion HITL primitive for agent-proposed changes
- Kubernetes Operator pattern docs
