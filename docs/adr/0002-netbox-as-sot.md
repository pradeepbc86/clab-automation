# ADR 0002 — NetBox as the Source of Truth

**Status:** Accepted
**Date:** 2026-05-16

## Context

A GitOps automation pipeline needs an authoritative store of intended state. Common options: NetBox, Nautobot, raw YAML/JSON in Git, Infrahub, a custom DB.

## Decision

NetBox. Generated configs depend on a SoT that exposes a REST API with at minimum: devices, device-types, platforms, sites, interfaces, IP addresses, and BGP peer relationships.

## Rationale

| Dimension | NetBox | Nautobot | Raw YAML in Git | Infrahub |
|-----------|--------|----------|-----------------|----------|
| Maturity | 2016+, large operator install base | 2020+, fork of NetBox | n/a | 2023+, newest |
| BGP modeling | Plugin (`netbox-bgp`) | Built-in BGP plugin | DIY | Schema-defined |
| Schema flexibility | Custom fields + JSON | First-class custom models | Total | First-class |
| Webhook + change events | Yes | Yes (more capable) | No | Yes |
| GitOps friendliness | API + ETL | API + Jobs | Native | Pull-from-Git |
| Community size | Largest | Growing | n/a | Smallest |

We chose NetBox because:
1. It's the de-facto operator standard for inventory + IPAM, so the lab demonstrates a real-world integration point.
2. The REST API contract is stable enough that this lab's `generate.py` doesn't need to track NetBox versions tightly.
3. Falling back to a YAML fixture (when no NetBox available) is straightforward — the SoT shape is small.

Nautobot is arguably the better choice for new builds today (better BGP modeling out of the box, native Job runner), but the muscle memory and operator familiarity favor NetBox.

## Trade-offs accepted

- NetBox's BGP modeling is plugin-dependent. The schema we use (`schemas/device.schema.json`) is what we *want* NetBox to produce after ETL, not necessarily NetBox's native model.
- NetBox is heavy (Django + PostgreSQL + Redis + RQ workers). Lab uses `netboxcommunity/netbox:latest` which bundles enough for a single-process demo.

## Migration path

If we outgrow NetBox: the `validate_devices()` step takes a list of dicts. Swap `get_devices_from_netbox()` for `get_devices_from_<x>()` and the rest of the pipeline is unchanged.
