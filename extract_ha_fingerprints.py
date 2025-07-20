#!/usr/bin/env python3
"""
Extract device fingerprints from Home Assistant components
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Set

def clone_home_assistant():
    """Clone Home Assistant core repository"""
    ha_dir = Path("home-assistant-core")
    
    if ha_dir.exists():
        print("✅ Home Assistant repository already exists")
        return ha_dir
    
    print("📥 Cloning Home Assistant core repository...")
    try:
        subprocess.run([
            "git", "clone", 
            "https://github.com/home-assistant/core.git", 
            "home-assistant-core"
        ], check=True)
        print("✅ Home Assistant repository cloned successfully")
        return ha_dir
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to clone Home Assistant: {e}")
        return None

def extract_zeroconf_patterns(ha_dir: Path) -> Dict[str, List[str]]:
    """Extract Zeroconf patterns from Home Assistant components"""
    patterns = {}
    components_dir = ha_dir / "homeassistant" / "components"
    
    print("🔍 Extracting Zeroconf patterns...")
    
    for component_dir in components_dir.iterdir():
        if not component_dir.is_dir():
            continue
            
        manifest_file = component_dir / "manifest.json"
        if not manifest_file.exists():
            continue
            
        try:
            with open(manifest_file, 'r') as f:
                manifest = json.load(f)
                
            zeroconf = manifest.get("zeroconf", [])
            if zeroconf:
                component_name = component_dir.name
                patterns[component_name] = zeroconf
                
        except (json.JSONDecodeError, KeyError):
            continue
    
    return patterns

def extract_dhcp_patterns(ha_dir: Path) -> Dict[str, Dict[str, List[str]]]:
    """Extract DHCP patterns from Home Assistant config flows"""
    patterns = {}
    components_dir = ha_dir / "homeassistant" / "components"
    
    print("🔍 Extracting DHCP patterns...")
    
    for component_dir in components_dir.iterdir():
        if not component_dir.is_dir():
            continue
            
        config_flow_file = component_dir / "config_flow.py"
        if not config_flow_file.exists():
            continue
            
        try:
            with open(config_flow_file, 'r') as f:
                content = f.read()
                
            # Look for DHCP discovery patterns
            dhcp_matches = re.findall(r'async_step_dhcp\([^)]*\)[^}]*?macaddress[^}]*?([^}]+)', content, re.DOTALL)
            hostname_matches = re.findall(r'async_step_dhcp\([^)]*\)[^}]*?hostname[^}]*?([^}]+)', content, re.DOTALL)
            
            if dhcp_matches or hostname_matches:
                component_name = component_dir.name
                patterns[component_name] = {
                    'macaddress': dhcp_matches,
                    'hostname': hostname_matches
                }
                
        except Exception:
            continue
    
    return patterns

def extract_ssdp_patterns(ha_dir: Path) -> Dict[str, Dict[str, List[str]]]:
    """Extract SSDP patterns from Home Assistant components"""
    patterns = {}
    components_dir = ha_dir / "homeassistant" / "components"
    
    print("🔍 Extracting SSDP patterns...")
    
    for component_dir in components_dir.iterdir():
        if not component_dir.is_dir():
            continue
            
        # Look in config_flow.py for SSDP patterns
        config_flow_file = component_dir / "config_flow.py"
        if config_flow_file.exists():
            try:
                with open(config_flow_file, 'r') as f:
                    content = f.read()
                    
                # Look for SSDP discovery patterns
                ssdp_matches = re.findall(r'urn:schemas-upnp-org:device:[^"]+', content)
                manufacturer_matches = re.findall(r'manufacturer[^}]*?([^}]+)', content, re.DOTALL)
                
                if ssdp_matches or manufacturer_matches:
                    component_name = component_dir.name
                    patterns[component_name] = {
                        'device_types': ssdp_matches,
                        'manufacturers': manufacturer_matches
                    }
                    
            except Exception:
                continue
    
    return patterns

def generate_fingerprint_summary():
    """Generate a summary of extracted fingerprints"""
    ha_dir = clone_home_assistant()
    if not ha_dir:
        return
    
    print("\n" + "="*60)
    print("🏠 HOME ASSISTANT FINGERPRINT EXTRACTION")
    print("="*60)
    
    # Extract patterns
    zeroconf_patterns = extract_zeroconf_patterns(ha_dir)
    dhcp_patterns = extract_dhcp_patterns(ha_dir)
    ssdp_patterns = extract_ssdp_patterns(ha_dir)
    
    # Generate summary
    print(f"\n📊 EXTRACTION SUMMARY:")
    print(f"   Zeroconf patterns: {len(zeroconf_patterns)} components")
    print(f"   DHCP patterns: {len(dhcp_patterns)} components")
    print(f"   SSDP patterns: {len(ssdp_patterns)} components")
    
    # Show popular components
    popular_components = [
        'amazon', 'samsung', 'philips', 'nest', 'lg', 'roomba', 
        'esphome', 'hue', 'alexa', 'fire', 'kindle', 'tv'
    ]
    
    print(f"\n🎯 POPULAR COMPONENTS FOUND:")
    for component in popular_components:
        found_components = []
        
        for comp_name in zeroconf_patterns.keys():
            if component.lower() in comp_name.lower():
                found_components.append(comp_name)
        
        if found_components:
            print(f"   {component.upper()}: {', '.join(found_components)}")
    
    # Show specific patterns for Amazon devices
    print(f"\n📱 AMAZON DEVICE PATTERNS:")
    amazon_components = [k for k in zeroconf_patterns.keys() if 'amazon' in k.lower()]
    
    for component in amazon_components:
        print(f"\n   {component.upper()}:")
        if component in zeroconf_patterns:
            for pattern in zeroconf_patterns[component]:
                print(f"     Zeroconf: {pattern}")
        if component in dhcp_patterns:
            dhcp = dhcp_patterns[component]
            if dhcp.get('macaddress'):
                print(f"     MAC: {dhcp['macaddress']}")
            if dhcp.get('hostname'):
                print(f"     Hostname: {dhcp['hostname']}")
    
    # Save to file
    output_file = "ha_fingerprints.json"
    with open(output_file, 'w') as f:
        json.dump({
            'zeroconf': zeroconf_patterns,
            'dhcp': dhcp_patterns,
            'ssdp': ssdp_patterns
        }, f, indent=2)
    
    print(f"\n💾 Fingerprints saved to: {output_file}")
    print(f"📖 Use this data to improve your device integrations!")

if __name__ == "__main__":
    generate_fingerprint_summary() 