#!/usr/bin/env python3
"""
Deploy BGP configs to devices using NAPALM (cEOS) and Netmiko (FRR).
Shows diff, applies config, and supports rollback.
"""

import sys
from napalm import get_network_driver
from netmiko import ConnectHandler

DEVICES = {
    'spine1': {'host': '127.0.0.1', 'driver': 'eos', 'port': 8001},
    'leaf1': {'host': '127.0.0.1', 'driver': 'eos', 'port': 8002},
    'leaf2': {'host': '127.0.0.1', 'driver': 'frr', 'port': 22},
}

def deploy_with_napalm(device_name, config_file):
    """Deploy config using NAPALM (for EOS)"""
    dev_info = DEVICES[device_name]
    driver = get_network_driver(dev_info['driver'])
    device = driver(
        hostname=dev_info['host'],
        username='admin',
        password='admin',
        port=dev_info['port'],
    )
    device.open()

    with open(config_file, 'r') as f:
        config = f.read()

    device.load_merge_candidate(config=config)
    print(f"\n--- Diff for {device_name} ---")
    print(device.compare_config())

    response = input(f"Deploy to {device_name}? [y/N]: ")
    if response.lower() == 'y':
        device.commit_config()
        print(f"✅ Config deployed to {device_name}")
    else:
        device.discard_config()
        print(f"❌ Rollback — config discarded")

    device.close()

def deploy_with_netmiko(device_name, config_file):
    """Deploy config using Netmiko (for FRR)"""
    net_connect = ConnectHandler(
        device_type='linux',
        host='127.0.0.1',
        username='root',
        port=2222,  # SSH port in ContainerLab
    )

    with open(config_file, 'r') as f:
        config = f.read()

    print(f"\n--- Config for {device_name} ---")
    print(config)

    response = input(f"Deploy to {device_name}? [y/N]: ")
    if response.lower() == 'y':
        output = net_connect.send_config_set(config.split('\n'))
        print(f"✅ Config deployed to {device_name}")
    else:
        print(f"❌ Skipped deployment")

    net_connect.disconnect()

def deploy(device_name):
    """Deploy config to a device"""
    if device_name not in DEVICES:
        print(f"Unknown device: {device_name}")
        return

    config_file = f"output/{device_name}.conf"

    try:
        driver = DEVICES[device_name]['driver']
        if driver == 'eos':
            deploy_with_napalm(device_name, config_file)
        else:  # frr
            deploy_with_netmiko(device_name, config_file)
    except Exception as e:
        print(f"Error deploying: {e}")

if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] != '--device':
        print("Usage: python deploy.py --device <device_name>")
        sys.exit(1)

    deploy(sys.argv[2])
