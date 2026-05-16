"""
Idempotency test for the config generator.

Render every device's config twice. Assert the bytes are byte-identical.
This catches:
  - Nondeterministic dict iteration (Python 3.7+ preserves insertion order
    so this is mostly defensive, but worth asserting)
  - Timestamp / UUID leaks into templates
  - Random ordering of peers (we sort them or accept input order)
  - Whitespace drift between renders

If this test ever fails, the pipeline is unsafe to re-run idempotently.
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "devices.json"
TEMPLATES_DIR = ROOT / "templates"

VENDOR_TEMPLATES = {
    "frr": "frr/bgp_peer.j2",
    "arista_eos": "arista/bgp_peer.j2",
    "juniper_junos": "juniper/bgp_peer.j2",
    "cisco_iosxr": "cisco/bgp_peer.j2",
}


def _render(device: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template(VENDOR_TEMPLATES[device["platform"]])
    return template.render(device=device)


@pytest.fixture(scope="module")
def devices():
    return json.loads(FIXTURES.read_text())


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_render_is_byte_identical(devices, idx):
    """Rendering the same device twice must produce identical bytes."""
    device = devices[idx]
    first = _render(device)
    second = _render(device)
    assert first == second, f"Non-idempotent render for {device['name']}"


def test_render_contains_required_fields(devices):
    """Every rendered config must include the device's ASN and at least one peer IP."""
    for d in devices:
        rendered = _render(d)
        assert str(d["asn"]) in rendered, f"ASN {d['asn']} missing from {d['name']}"
        for peer in d["peers"]:
            assert peer["ip"] in rendered, f"Peer {peer['ip']} missing from {d['name']}"
