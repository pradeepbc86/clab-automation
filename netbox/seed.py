#!/usr/bin/env python3
"""
Populate Netbox with sample devices and BGP peer data.
"""

import os
import requests

NETBOX_URL = os.getenv('NETBOX_URL', 'http://localhost:8000')
NETBOX_TOKEN = os.getenv('NETBOX_TOKEN', 'your-token-here')

headers = {'Authorization': f'Token {NETBOX_TOKEN}', 'Content-Type': 'application/json'}

devices = [
    {'name': 'spine1', 'device_type': 1, 'site': 1, 'platform': 1, 'asn': 65000},
    {'name': 'leaf1', 'device_type': 1, 'site': 1, 'platform': 1, 'asn': 65001},
    {'name': 'leaf2', 'device_type': 1, 'site': 1, 'platform': 2, 'asn': 65002},
]

def seed_devices():
    """Create sample devices in Netbox"""
    for device in devices:
        url = f'{NETBOX_URL}/api/dcim/devices/'
        try:
            resp = requests.post(url, json=device, headers=headers, timeout=10)
            if resp.status_code in [200, 201]:
                print(f"✅ Created device: {device['name']}")
            else:
                print(f"⚠️  Device {device['name']}: {resp.status_code}")
        except Exception as e:
            print(f"Error creating {device['name']}: {e}")

if __name__ == '__main__':
    seed_devices()
