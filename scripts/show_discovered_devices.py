#!/usr/bin/env python3
"""
Show all discovered devices from network discovery
"""

import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    """Display all discovered devices"""
    try:
        with open('network_discovery_results.json', 'r') as f:
            data = json.load(f)
        
        print("🔍 NETWORK DISCOVERY RESULTS")
        print("=" * 80)
        print(f"Total devices found: {data['total_devices']}")
        print(f"Smart devices: {data['smart_devices']}")
        print(f"Jarvis nodes: {data['jarvis_nodes']}")
        print()
        
        # Show smart devices
        if data['devices']['smart_devices']:
            print("💡 SMART DEVICES:")
            print("-" * 40)
            for i, device in enumerate(data['devices']['smart_devices'], 1):
                print(f"{i:2d}. {device['ip_address']:15} | {device['device_type']:20} | {device.get('manufacturer', '-')}")
                if device.get('hostname'):
                    print(f"     Hostname: {device['hostname']}")
                if device.get('mac_address'):
                    print(f"     MAC: {device['mac_address']}")
                if device.get('open_ports'):
                    print(f"     Ports: {device['open_ports']}")
                print()
        
        # Show other devices
        if data['devices']['other_devices']:
            print("📱 OTHER DEVICES:")
            print("-" * 40)
            for i, device in enumerate(data['devices']['other_devices'], 1):
                print(f"{i:2d}. {device['ip_address']:15} | {device['device_type']:20} | {device.get('manufacturer', '-')}")
                if device.get('hostname'):
                    print(f"     Hostname: {device['hostname']}")
                if device.get('mac_address'):
                    print(f"     MAC: {device['mac_address']}")
                if device.get('open_ports'):
                    print(f"     Ports: {device['open_ports']}")
                print()
        
        # Show Jarvis nodes
        if data['devices']['jarvis_nodes']:
            print("🤖 JARVIS NODES:")
            print("-" * 40)
            for i, device in enumerate(data['devices']['jarvis_nodes'], 1):
                print(f"{i:2d}. {device['ip_address']:15} | {device['device_type']:20} | {device.get('manufacturer', '-')}")
                if device.get('hostname'):
                    print(f"     Hostname: {device['hostname']}")
                if device.get('mac_address'):
                    print(f"     MAC: {device['mac_address']}")
                if device.get('open_ports'):
                    print(f"     Ports: {device['open_ports']}")
                print()
        
        print("✅ Device discovery complete!")
        
    except FileNotFoundError:
        print("❌ No discovery results found. Run the network discovery first!")
    except Exception as e:
        print(f"❌ Error reading discovery results: {e}")

if __name__ == "__main__":
    main() 