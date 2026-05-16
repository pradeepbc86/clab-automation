"""SoT schema validation tests."""

import json
from pathlib import Path

import pytest

try:
    import jsonschema
except ImportError:
    pytest.skip("jsonschema not installed", allow_module_level=True)

ROOT = Path(__file__).parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "device.schema.json").read_text())
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "devices.json").read_text())


def test_fixture_devices_pass_schema():
    """All sample devices must conform to the schema."""
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for d in FIXTURES:
        errors = list(validator.iter_errors(d))
        assert not errors, f"{d['name']} failed: {[e.message for e in errors]}"


def test_missing_required_field_fails():
    validator = jsonschema.Draft202012Validator(SCHEMA)
    bad = {"name": "spine1", "platform": "frr"}  # missing asn, router_id, peers
    errors = list(validator.iter_errors(bad))
    assert len(errors) >= 3


def test_invalid_asn_rejected():
    validator = jsonschema.Draft202012Validator(SCHEMA)
    bad = {
        "name": "spine1", "platform": "frr",
        "asn": 5_000_000_000,  # over 32-bit max
        "router_id": "10.0.0.1",
        "peers": [{"ip": "10.10.1.2", "asn": 65000, "description": "x"}],
    }
    errors = list(validator.iter_errors(bad))
    assert any("4294967295" in e.message or "maximum" in e.message.lower() for e in errors)


def test_bad_hostname_rejected():
    validator = jsonschema.Draft202012Validator(SCHEMA)
    bad = {
        "name": "Bad-NAME-with-UPPER",
        "platform": "frr", "asn": 65001, "router_id": "10.0.0.1",
        "peers": [{"ip": "10.10.1.2", "asn": 65000, "description": "x"}],
    }
    errors = list(validator.iter_errors(bad))
    assert any("does not match" in e.message for e in errors)
