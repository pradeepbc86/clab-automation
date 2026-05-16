#!/usr/bin/env python3
"""
Validate BGP session state on all devices post-deployment.
"""

import subprocess
import sys

def check_bgp_state(device_name):
    """Check BGP neighbors are Established"""
    cmd = f"sudo docker exec clab-bgp-config-automation-{device_name} vtysh -c 'show bgp summary' 2>/dev/null"
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr

        if 'Established' in output:
            print(f"✅ {device_name}: BGP Established")
            return True
        else:
            print(f"❌ {device_name}: BGP NOT Established")
            return False
    except Exception as e:
        print(f"❌ {device_name}: Error checking BGP ({e})")
        return False

def validate_all():
    """Validate all devices"""
    devices = ['spine1', 'leaf1', 'leaf2']
    all_pass = True

    print("=== BGP State Validation ===\n")
    for device in devices:
        if not check_bgp_state(device):
            all_pass = False

    print()
    if all_pass:
        print("✅ All devices validated")
        return 0
    else:
        print("❌ Validation failed")
        return 1

if __name__ == '__main__':
    sys.exit(validate_all())
