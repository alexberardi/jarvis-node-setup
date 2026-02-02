#!/usr/bin/env python3
"""Test AP mode directly to debug hostapd/dnsmasq issues."""

import os
import sys

# Force hostapd backend
os.environ['JARVIS_WIFI_BACKEND'] = 'hostapd'

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provisioning.wifi_manager import HostapdWiFiManager

def main():
    print("=" * 50)
    print("AP Mode Test")
    print("=" * 50)

    wifi = HostapdWiFiManager()

    print("\nStarting AP mode...")
    result = wifi.start_ap_mode('jarvis-test')

    print(f"\nResult: {result}")

    if result:
        print("\n" + "=" * 50)
        print("AP should be broadcasting: jarvis-test")
        print("Check your phone's WiFi list!")
        print("=" * 50)
        print("\nPress Enter to stop AP mode...")
        try:
            input()
        except KeyboardInterrupt:
            pass
        wifi.stop_ap_mode()
    else:
        print("\nAP mode failed to start. Check errors above.")

if __name__ == "__main__":
    main()
