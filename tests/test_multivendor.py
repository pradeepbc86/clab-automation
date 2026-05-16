"""
Multi-vendor template render tests.

The cisco_iosxr and juniper_junos templates exist as renderable references —
even though our lab topology runs cEOS + FRR, the GitOps pipeline supports
all four vendors. These tests prove the templates produce syntactically
plausible configs from the same SoT data.
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "multivendor_devices.json"
TEMPLATES_DIR = ROOT / "templates"

VENDOR_TEMPLATES = {
    "juniper_junos": "juniper/bgp_peer.j2",
    "cisco_iosxr":   "cisco/bgp_peer.j2",
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
def multivendor_devices():
    return json.loads(FIXTURES.read_text())


def test_juniper_renders(multivendor_devices):
    juniper = next(d for d in multivendor_devices if d["platform"] == "juniper_junos")
    out = _render(juniper)
    assert "protocols" in out
    assert "bgp" in out
    assert str(juniper["asn"]) in out
    for peer in juniper["peers"]:
        assert peer["ip"] in out
        assert str(peer["asn"]) in out


def test_cisco_renders(multivendor_devices):
    cisco = next(d for d in multivendor_devices if d["platform"] == "cisco_iosxr")
    out = _render(cisco)
    assert "router bgp" in out
    assert str(cisco["asn"]) in out
    for peer in cisco["peers"]:
        assert peer["ip"] in out


def test_multivendor_idempotent(multivendor_devices):
    """Each multi-vendor device renders idempotently."""
    for device in multivendor_devices:
        first = _render(device)
        second = _render(device)
        assert first == second
