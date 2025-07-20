from typing import Dict, Any, List
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter


class RoomParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "room"
    
    @property
    def param_type(self) -> str:
        return "str"
    
    @property
    def description(self) -> str:
        return "The room where the lights should be controlled"
    
    @property
    def validation_function(self):
        def validate_room(room: str) -> bool:
            valid_rooms = ["kitchen", "living_room", "bedroom", "office", "bathroom"]
            return room.lower() in valid_rooms
        return validate_room
    
    @property
    def validation_error_message(self) -> str:
        return "Room must be one of: kitchen, living_room, bedroom, office, bathroom"


class BrightnessParameter(IJarvisParameter):
    @property
    def name(self) -> str:
        return "brightness"
    
    @property
    def param_type(self) -> str:
        return "int"
    
    @property
    def description(self) -> str:
        return "Brightness level from 0 (off) to 100 (full brightness)"
    
    @property
    def required(self) -> bool:
        return False
    
    @property
    def default_value(self) -> int:
        return 100
    
    @property
    def validation_function(self):
        def validate_brightness(brightness: int) -> bool:
            return 0 <= brightness <= 100
        return validate_brightness
    
    @property
    def validation_error_message(self) -> str:
        return "Brightness must be between 0 and 100"


class TurnOnLightsCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "turn_on_lights"
    
    @property
    def description(self) -> str:
        return "Turn on lights in a specific room with optional brightness control"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "turn on", "turn on lights", "lights on", "switch on lights", "enable lights",
            "light up", "brighten", "illuminate", "activate lights", "power on lights",
            "turn on the lights", "switch on the lights", "turn on light", "switch on light"
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [RoomParameter(), BrightnessParameter()]
    
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        room = params.get("room")
        brightness = params.get("brightness", 100)
        
        try:
            # Simulate light control (replace with actual implementation)
            print(f"🔆 Turning on lights in {room} at {brightness}% brightness")
            
            # Here you would integrate with your actual light control system
            # For example: homeassistant_client.turn_on_lights(room, brightness)
            
            return {
                "success": True,
                "message": f"Lights turned on in {room} at {brightness}% brightness",
                "data": {
                    "room": room,
                    "brightness": brightness,
                    "status": "on"
                },
                "errors": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "error_code": "light_control_error",
                "message": f"Failed to turn on lights in {room}: {str(e)}",
                "data": None,
                "errors": {"light_control": str(e)}
            }


class TurnOffLightsCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "turn_off_lights"
    
    @property
    def description(self) -> str:
        return "Turn off lights in a specific room"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "turn off", "turn off lights", "lights off", "switch off lights", "disable lights",
            "turn out", "darken", "deactivate lights", "power off lights", "shut off lights",
            "turn off the lights", "switch off the lights", "turn off light", "switch off light",
            "kill the lights", "shut down lights"
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [RoomParameter()]
    
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        room = params.get("room")
        
        try:
            # Simulate light control (replace with actual implementation)
            print(f"🔇 Turning off lights in {room}")
            
            # Here you would integrate with your actual light control system
            # For example: homeassistant_client.turn_off_lights(room)
            
            return {
                "success": True,
                "message": f"Lights turned off in {room}",
                "data": {
                    "room": room,
                    "status": "off"
                },
                "errors": None
            }
            
        except Exception as e:
            return {
                "success": False,
                "error_code": "light_control_error",
                "message": f"Failed to turn off lights in {room}: {str(e)}",
                "data": None,
                "errors": {"light_control": str(e)}
            } 