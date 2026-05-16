"""
Cross-project test: automation CAN render the configs that the clab-fabric-evpn
repo ships by hand. This proves the schema + templates aren't decoration —
they actually model the EVPN/BFD/policy depth the fabric demands.
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parent.parent
FABRIC_LEAF1_FIXTURE = ROOT / "tests" / "fixtures" / "fabric_evpn_leaf1.json"


def _render(device: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.get_template("frr/bgp_peer.j2").render(device=device)


@pytest.fixture
def leaf1_device():
    return json.loads(FABRIC_LEAF1_FIXTURE.read_text())


def test_renders_at_all(leaf1_device):
    out = _render(leaf1_device)
    assert out, "empty render"
    assert "router bgp 4200000003" in out


def test_renders_loopback(leaf1_device):
    out = _render(leaf1_device)
    assert "interface lo" in out
    assert "ip address 10.0.0.3/32" in out


def test_renders_all_interfaces(leaf1_device):
    out = _render(leaf1_device)
    for iface in leaf1_device["interfaces"]:
        assert iface["name"] in out
        assert iface["ip"] in out
        assert f"mtu {iface['mtu']}" in out


def test_renders_bfd_profile(leaf1_device):
    out = _render(leaf1_device)
    assert "bfd" in out
    assert "profile FABRIC" in out
    assert "detect-multiplier 3" in out
    assert "receive-interval 150" in out
    assert "transmit-interval 150" in out


def test_renders_graceful_restart(leaf1_device):
    out = _render(leaf1_device)
    assert "bgp graceful-restart" in out
    assert "bgp graceful-restart preserve-fw-state" in out
    assert "bgp graceful-restart stalepath-time 300" in out
    assert "bgp graceful-restart restart-time 120" in out


def test_renders_evpn_afi(leaf1_device):
    out = _render(leaf1_device)
    assert "address-family l2vpn evpn" in out
    assert "advertise-all-vni" in out
    assert "advertise-default-gw" in out


def test_renders_prefix_lists(leaf1_device):
    out = _render(leaf1_device)
    assert "ip prefix-list PL-FABRIC-LOOPBACKS" in out
    assert "10.0.0.0/24" in out


def test_renders_route_maps(leaf1_device):
    out = _render(leaf1_device)
    assert "route-map RM-SPINES-IN permit 10" in out
    assert "route-map RM-SPINES-OUT permit 10" in out
    assert "match ip address prefix-list PL-FABRIC-LOOPBACKS" in out


def test_renders_peer_group_with_bfd(leaf1_device):
    out = _render(leaf1_device)
    assert "neighbor SPINES peer-group" in out
    assert "neighbor SPINES remote-as external" in out
    assert "neighbor SPINES bfd" in out
    assert "neighbor SPINES bfd profile FABRIC" in out


def test_renders_per_peer_attributes(leaf1_device):
    out = _render(leaf1_device)
    for peer in leaf1_device["peers"]:
        assert peer["ip"] in out
        assert peer["description"] in out


def test_renders_max_prefix_and_route_maps(leaf1_device):
    out = _render(leaf1_device)
    assert "maximum-prefix 1000 80 restart 5" in out
    assert "neighbor SPINES route-map RM-SPINES-IN in" in out
    assert "neighbor SPINES route-map RM-SPINES-OUT out" in out


def test_renders_idempotently(leaf1_device):
    first = _render(leaf1_device)
    second = _render(leaf1_device)
    assert first == second


def test_basic_simple_device_still_renders():
    """Regression: minimal device (no evpn/bfd/policies) must still render."""
    minimal = {
        "name": "spine1", "platform": "frr", "asn": 65000,
        "router_id": "10.0.0.1",
        "peers": [{"ip": "10.10.1.2", "asn": 65001, "description": "leaf1"}],
    }
    out = _render(minimal)
    assert "router bgp 65000" in out
    assert "10.10.1.2" in out
    assert "l2vpn evpn" not in out
