#!/usr/bin/env python3
"""
Generate BGP peer configs from Netbox SoT using Jinja2 templates.
Pulls device/peer data from Netbox REST API and renders per-vendor templates.
"""

import os
import sys
import json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
import requests

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

NETBOX_URL = os.getenv('NETBOX_URL', 'http://localhost:8000')
NETBOX_TOKEN = os.getenv('NETBOX_TOKEN')
if not NETBOX_TOKEN:
    print("Warning: NETBOX_TOKEN not set — falling back to sample data")

SCHEMA_PATH = Path(__file__).parent / 'schemas' / 'device.schema.json'


def validate_devices(devices: list[dict]) -> list[dict]:
    """Validate each device against JSON Schema. Fail loud on invalid input."""
    if not HAS_JSONSCHEMA:
        print("Warning: jsonschema not installed — skipping SoT validation")
        return devices
    if not SCHEMA_PATH.exists():
        print(f"Warning: {SCHEMA_PATH} missing — skipping validation")
        return devices
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    errors = []
    for d in devices:
        for err in validator.iter_errors(d):
            errors.append(f"{d.get('name','<unnamed>')}: {err.message} at {list(err.path)}")
    if errors:
        print("SoT validation failed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    return devices

def get_devices_from_netbox():
    """Fetch all devices and BGP peers from Netbox"""
    headers = {'Authorization': f'Token {NETBOX_TOKEN}'}
    url = f'{NETBOX_URL}/api/dcim/devices/?limit=0'
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get('results', [])
    except Exception as e:
        print(f"Error fetching from Netbox: {e}")
        return []

def render_config(device, template_path):
    """Render Jinja2 template for a device"""
    env = Environment(loader=FileSystemLoader('templates'))
    try:
        template = env.get_template(template_path)
        return template.render(device=device)
    except Exception as e:
        print(f"Error rendering template {template_path}: {e}")
        return ""

def generate_configs(device_name=None):
    """Generate configs for all or specified device"""
    devices = get_devices_from_netbox()
    devices = validate_devices(devices)

    if not devices:
        print("No devices found in Netbox. Using sample data.")
        devices = [
            {
                'name': 'spine1', 'platform': 'arista_eos', 'asn': 65000,
                'router_id': '10.0.0.1',
                'peers': [
                    {'ip': '10.10.1.2', 'asn': 65001, 'description': 'leaf1'},
                    {'ip': '10.10.2.2', 'asn': 65002, 'description': 'leaf2'},
                ]
            },
            {
                'name': 'leaf1', 'platform': 'arista_eos', 'asn': 65001,
                'router_id': '10.0.0.3',
                'peers': [
                    {'ip': '10.10.1.1', 'asn': 65000, 'description': 'spine1'},
                    {'ip': '10.10.3.1', 'asn': 65000, 'description': 'spine2'},
                ]
            },
            {
                'name': 'leaf2', 'platform': 'frr', 'asn': 65002,
                'router_id': '10.0.0.4',
                'peers': [
                    {'ip': '10.10.2.1', 'asn': 65000, 'description': 'spine1'},
                    {'ip': '10.10.4.1', 'asn': 65000, 'description': 'spine2'},
                ]
            },
        ]

    template_map = {
        'arista_eos': 'arista/bgp_peer.j2',
        'frr': 'frr/bgp_peer.j2',
        'juniper_junos': 'juniper/bgp_peer.j2',
        'cisco_iosxr': 'cisco/bgp_peer.j2',
    }

    Path('output').mkdir(exist_ok=True)

    for device in devices:
        if device_name and device.get('name') != device_name:
            continue

        platform = device.get('platform', 'frr')
        template_file = template_map.get(platform, 'frr/bgp_peer.j2')

        config = render_config(device, template_file)
        output_file = f"output/{device.get('name')}.conf"

        with open(output_file, 'w') as f:
            f.write(config)

        print(f"✅ Generated: {output_file}")

if __name__ == '__main__':
    device_name = sys.argv[1] if len(sys.argv) > 1 else None
    if device_name == '--all':
        device_name = None
    generate_configs(device_name)
