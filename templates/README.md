# Templates

Per-vendor Jinja2 templates rendering BGP/EVPN configs from the SoT schema in `schemas/device.schema.json`.

## Feature coverage

| Feature | frr | arista | juniper | cisco |
|---------|:---:|:------:|:-------:|:-----:|
| Basic BGP peering | ✅ | ✅ | ✅ | ✅ |
| Peer groups | ✅ | ✅ | ✅ | ✅ |
| max-prefix / maximum-routes | ✅ | ✅ | ✅ | ✅ |
| Interfaces (`device.interfaces`) | ✅ | ✅ | ⚪ | ⚪ |
| Loopback | ✅ | ✅ | ⚪ | ⚪ |
| BFD | ✅ | ✅ | ⚪ | ⚪ |
| Graceful Restart | ✅ | ✅ | ⚪ | ⚪ |
| Prefix-lists | ✅ | ✅ | ⚪ | ⚪ |
| Route-maps with set-actions | ✅ | ✅ | ⚪ | ⚪ |
| EVPN AFI | ✅ | ✅ | ⚪ | ⚪ |
| Large communities | ✅ | ✅ | ⚪ | ⚪ |

✅ = full schema coverage  ⚪ = basic BGP only; richer fields ignored

## Why the asymmetry

The `frr` and `arista` templates render every schema block — they're the
"reference" implementations and what the test suite exercises against the
`clab-fabric-evpn` topology. The `juniper` and `cisco` templates are
deliberately minimal: they prove the schema is vendor-neutral enough to
target other platforms, but the per-vendor depth (RIB-OUT policy syntax,
EVPN route-target schemas, BGP graceful restart command differences) hasn't
been built out.

Extending `juniper/bgp_peer.j2` and `cisco/bgp_peer.j2` to feature parity
is straightforward — the schema already has the data; the templates need
the per-platform syntax. Left as a deliberate scope choice for this lab.

## Adding a new vendor

1. Add the platform to `schemas/device.schema.json` `properties.platform.enum`
2. Create `templates/<vendor>/bgp_peer.j2`
3. Add an entry to `generate.py`'s `template_map`
4. Add a fixture in `tests/fixtures/`
5. Add a test in `tests/test_<vendor>.py` asserting expected output
