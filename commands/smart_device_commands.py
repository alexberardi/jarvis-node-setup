#!/usr/bin/env python3
"""
Smart Device Commands - Integrates with the discovery system
"""

from typing import Dict, Any, List, Optional
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter
from utils.device_detectors import device_registry
import requests
import json
import subprocess


class DeviceParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "device_name"
    
    @property
    def param_type(self) -> str:
        return "str"
    
    @property
    def description(self) -> str:
        return "Name or type of device (e.g., 'nest thermostat', 'roomba', 'lg tv')"
    
    @property
    def required(self) -> bool:
        return True


class TemperatureParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "temperature"
    
    @property
    def param_type(self) -> str:
        return "int"
    
    @property
    def description(self) -> str:
        return "Target temperature in Fahrenheit (60-85)"
    
    @property
    def validation_function(self):
        def validate_temp(temp: int) -> bool:
            return 60 <= temp <= 85
        return validate_temp
    
    @property
    def validation_error_message(self) -> str:
        return "Temperature must be between 60 and 85 degrees Fahrenheit"


class ControlNestThermostatCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "control_nest_thermostat"
    
    @property
    def description(self) -> str:
        return "Control the Nest thermostat temperature"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "set temperature", "adjust thermostat", "change temperature", 
            "set thermostat", "nest thermostat", "heating", "cooling",
            "make it warmer", "make it cooler", "set temp"
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [TemperatureParameter()]
    
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        temperature = params.get("temperature")
        
        try:
            # Find Nest thermostat in discovered devices
            thermostat_ip = self._find_nest_thermostat()
            
            if not thermostat_ip:
                return {
                    "success": False,
                    "error_code": "device_not_found",
                    "message": "Nest thermostat not found on network",
                    "data": None,
                    "errors": {"discovery": "No Nest thermostat discovered"}
                }
            
            # In a real implementation, you would use Google Nest API
            # For now, we'll simulate the command
            print(f"🌡️ Setting Nest thermostat at {thermostat_ip} to {temperature}°F")
            
            # Simulate API call (replace with actual Nest API)
            # result = self._call_nest_api(thermostat_ip, temperature)
            
            return {
                "success": True,
                "message": f"Nest thermostat set to {temperature}°F",
                "data": {
                    "device_ip": thermostat_ip,
                    "temperature": temperature,
                    "device_type": "nest_thermostat"
                },
                "errors": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "error_code": "thermostat_control_error",
                "message": f"Failed to control thermostat: {str(e)}",
                "data": None,
                "errors": {"control": str(e)}
            }
    
    def _find_nest_thermostat(self) -> Optional[str]:
        """Find Nest thermostat IP from bulletproof discovery results"""
        try:
            # Try bulletproof discovery first (most reliable)
            with open('bulletproof_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'nest_thermostat':
                    return ip
            
            # Fallback to lightweight discovery
            with open('lightweight_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'nest_thermostat':
                    return ip
            
            return None
        except:
            return None


class ControlRoombaCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "control_roomba"
    
    @property
    def description(self) -> str:
        return "Start or stop the Roomba vacuum cleaner"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "start roomba", "stop roomba", "vacuum", "start cleaning",
            "stop cleaning", "roomba start", "roomba stop", "run roomba"
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [RoombaActionParameter()]
    
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        action = params.get("action", "start")
        
        try:
            # Find Roomba in discovered devices
            roomba_ip = self._find_roomba()
            
            if not roomba_ip:
                return {
                    "success": False,
                    "error_code": "device_not_found",
                    "message": "Roomba not found on network",
                    "data": None,
                    "errors": {"discovery": "No Roomba discovered"}
                }
            
            # In a real implementation, you would use iRobot API or MQTT
            print(f"🤖 {action.title()}ing Roomba at {roomba_ip}")
            
            # Simulate MQTT command to Roomba
            # self._send_roomba_mqtt_command(roomba_ip, action)
            
            return {
                "success": True,
                "message": f"Roomba {action} command sent",
                "data": {
                    "device_ip": roomba_ip,
                    "action": action,
                    "device_type": "roomba"
                },
                "errors": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "error_code": "roomba_control_error",
                "message": f"Failed to control Roomba: {str(e)}",
                "data": None,
                "errors": {"control": str(e)}
            }
    
    def _find_roomba(self) -> Optional[str]:
        """Find Roomba IP from bulletproof discovery results"""
        try:
            # Try bulletproof discovery first (most reliable)
            with open('bulletproof_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'roomba':
                    return ip
            
            # Fallback to lightweight discovery
            with open('lightweight_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'roomba':
                    return ip
            
            return None
        except:
            return None


class RoombaActionParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "action"
    
    @property
    def param_type(self) -> str:
        return "str"
    
    @property
    def description(self) -> str:
        return "Action to perform: 'start' or 'stop'"
    
    @property
    def validation_function(self):
        def validate_action(action: str) -> bool:
            return action.lower() in ['start', 'stop']
        return validate_action
    
    @property
    def validation_error_message(self) -> str:
        return "Action must be 'start' or 'stop'"


class ControlLGTVCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "control_lg_tv"
    
    @property
    def description(self) -> str:
        return "Control the LG TV (power on/off, volume, etc.)"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "turn on tv", "turn off tv", "tv on", "tv off", "television",
            "lg tv", "tv volume", "mute tv", "unmute tv"
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [TVActionParameter()]
    
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        action = params.get("action", "power_on")
        
        try:
            # Find LG TV in discovered devices
            tv_ip = self._find_lg_tv()
            
            if not tv_ip:
                return {
                    "success": False,
                    "error_code": "device_not_found",
                    "message": "LG TV not found on network",
                    "data": None,
                    "errors": {"discovery": "No LG TV discovered"}
                }
            
            # Use LG WebOS API
            result = self._send_webos_command(tv_ip, action)
            
            return {
                "success": True,
                "message": f"LG TV {action} command sent",
                "data": {
                    "device_ip": tv_ip,
                    "action": action,
                    "device_type": "lg_tv",
                    "api_response": result
                },
                "errors": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "error_code": "tv_control_error",
                "message": f"Failed to control LG TV: {str(e)}",
                "data": None,
                "errors": {"control": str(e)}
            }
    
    def _find_lg_tv(self) -> Optional[str]:
        """Find LG TV IP from bulletproof discovery results"""
        try:
            # Try bulletproof discovery first (most reliable)
            with open('bulletproof_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'lg_tv':
                    return ip
            
            # Fallback to lightweight discovery
            with open('lightweight_discovery_results.json', 'r') as f:
                results = json.load(f)
            
            for ip, device in results['devices'].items():
                if device.get('device_type') == 'lg_tv':
                    return ip
            
            return None
        except:
            return None
    
    def _send_webos_command(self, tv_ip: str, action: str) -> Dict[str, Any]:
        """Send WebOS API command to LG TV"""
        try:
            # Example WebOS API calls (simplified)
            webos_commands = {
                "power_on": {"type": "request", "id": "power_on", "uri": "ssap://system/turnOn"},
                "power_off": {"type": "request", "id": "power_off", "uri": "ssap://system/turnOff"},
                "volume_up": {"type": "request", "id": "volume_up", "uri": "ssap://audio/volumeUp"},
                "volume_down": {"type": "request", "id": "volume_down", "uri": "ssap://audio/volumeDown"},
                "mute": {"type": "request", "id": "mute", "uri": "ssap://audio/setMute", "payload": {"mute": True}}
            }
            
            command = webos_commands.get(action)
            if not command:
                return {"error": f"Unknown action: {action}"}
            
            # In real implementation, you would use WebSocket connection to WebOS
            print(f"📺 Sending WebOS command to {tv_ip}: {command}")
            
            # Simulate API response
            return {"status": "success", "command": action}
            
        except Exception as e:
            return {"error": str(e)}


class TVActionParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "action"
    
    @property
    def param_type(self) -> str:
        return "str"
    
    @property
    def description(self) -> str:
        return "TV action: power_on, power_off, volume_up, volume_down, mute"
    
    @property
    def validation_function(self):
        def validate_action(action: str) -> bool:
            valid_actions = ["power_on", "power_off", "volume_up", "volume_down", "mute"]
            return action.lower() in valid_actions
        return validate_action
    
    @property
    def validation_error_message(self) -> str:
        return "Action must be one of: power_on, power_off, volume_up, volume_down, mute" 