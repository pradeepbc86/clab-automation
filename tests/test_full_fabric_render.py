"""
End-to-end test: automation can render the ENTIRE clab-fabric-evpn fabric
from the SoT schema, not just leaf1. Proves the integration claim isn't a
sample size of 1.

Also exercises:
- Unnumbered devices (interfaces with no `ip` field)
- Large-community attach via route-map set-actions
"""

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _render_frr(device: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.get_template("frr/bgp_peer.j2").render(device=device)


@pytest.mark.parametrize("node", ["spine1", "spine2", "leaf1", "leaf2"])
def test_full_fabric_each_node_renders(node):
    """All 4 fabric nodes must render with EVPN/BFD/policies present."""
    fixture = FIXTURES / f"fabric_evpn_{node}.json"
    device = json.loads(fixture.read_text())
    out = _render_frr(device)

    assert f"router bgp {device['asn']}" in out
    assert "bgp router-id" in out
    assert "bgp graceful-restart" in out
    assert "bfd" in out
    assert "profile FABRIC" in out
    assert "maximum-paths 64" in out

    assert device["loopback"] in out
    for iface in device["interfaces"]:
        assert iface["ip"] in out

    group = device["peers"][0]["group"]
    assert f"neighbor {group} peer-group" in out
    for peer in device["peers"]:
        assert peer["ip"] in out
        assert peer["description"] in out


def test_leaves_advertise_all_vni_and_default_gw():
    """Only leaves should carry the anycast-gateway EVPN flags."""
    for node in ["leaf1", "leaf2"]:
        device = json.loads((FIXTURES / f"fabric_evpn_{node}.json").read_text())
        out = _render_frr(device)
        assert "advertise-all-vni" in out
        assert "advertise-default-gw" in out


def test_spines_do_not_advertise_default_gw():
    """Spines re-advertise EVPN routes but don't anycast-gateway themselves."""
    for node in ["spine1", "spine2"]:
        device = json.loads((FIXTURES / f"fabric_evpn_{node}.json").read_text())
        out = _render_frr(device)
        assert "advertise-default-gw" not in out


def test_unnumbered_device_renders_without_interface_ips():
    """Interface with no `ip` field must not render an `ip address` line."""
    device = json.loads((FIXTURES / "fabric_evpn_leaf1_unnumbered.json").read_text())
    out = _render_frr(device)

    assert "10.0.0.3/32" in out
    assert out.count("ip address 10.0.0.3/32") == 1
    assert "ip address 10.10.1." not in out
    assert "ip address 10.10.3." not in out


def test_large_community_renders():
    """Community schema → rendered config: set large-community attaches."""
    device = json.loads((FIXTURES / "leaf_with_community.json").read_text())
    out = _render_frr(device)
    assert "set large-community 4200000099:1:1 4200000099:2:2" in out
    assert "route-map RM-SPINES-OUT permit 10" in out
    assert "match ip address prefix-list PL-LOCAL-SERVERS" in out


def test_4node_fabric_asn_uniqueness():
    """Sanity: every fabric node has a distinct 4-byte ASN from the 4200000000+ range."""
    asns = set()
    for node in ["spine1", "spine2", "leaf1", "leaf2"]:
        device = json.loads((FIXTURES / f"fabric_evpn_{node}.json").read_text())
        assert device["asn"] >= 4_200_000_000
        assert device["asn"] not in asns
        asns.add(device["asn"])
    assert len(asns) == 4
