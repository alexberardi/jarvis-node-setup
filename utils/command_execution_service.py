import json
import httpx
from typing import Dict, Any, Optional, List
from clients.rest_client import RestClient
from utils.config_service import Config
from utils.command_discovery_service import get_command_discovery_service
from core.helpers import get_tts_provider


class CommandExecutionService:
    def __init__(self):
        self.command_center_url = Config.get_str("jarvis_command_center")
        self.node_id = Config.get_str("node_id")
        self.room = Config.get_str("room")
        self.command_discovery = get_command_discovery_service()
        # Force initial discovery
        self.command_discovery.refresh_now()

    def process_voice_command(self, voice_command: str) -> Dict[str, Any]:
        """
        Process a voice command through the complete pipeline:
        1. Send to Jarvis Command Center for LLM processing
        2. Execute the returned command locally
        3. Handle any errors conversationally
        
        Args:
            voice_command: The transcribed voice command
            
        Returns:
            Execution result dictionary
        """
        try:
            # Get available commands schema
            available_commands = self.command_discovery.get_available_commands_schema()
            print(f"🔍 Available commands: {len(available_commands)} commands found")
            
            # Prepare request payload
            payload = {
                "voice_command": voice_command,
                "node_context": {
                    "room": self.room,
                    "node_id": self.node_id
                },
                "available_commands": available_commands
            }
            
            print(f"📡 Sending to Command Center: {voice_command}")
            print(f"📦 Payload available_commands: {available_commands}")
            
            # Send to Jarvis Command Center
            response = RestClient.post(
                f"{self.command_center_url}/voice/command",
                data=payload,
                timeout=30
            )
            
            if not response:
                return self._handle_error("Failed to communicate with Command Center")
            
            # Extract command details from response
            commands = response.get("commands", [])
            
            if not commands:
                return self._handle_error("No commands specified in response")
            
            # For now, just speak the command name of the first successful command
            first_command = commands[0]
            if first_command.get("success"):
                command_name = first_command.get("command_name", "unknown command")
                parameters = first_command.get("parameters", {})
                
                # For testing purposes, still execute the command if it exists
                # In production, this would just speak the command name
                command = self.command_discovery.get_command(command_name)
                if command:
                    return self._execute_command(command_name, parameters)
                else:
                    # Just speak the command name
                    return {
                        "success": True,
                        "message": f"Command: {command_name}",
                        "data": first_command,
                        "errors": None
                    }
            else:
                return self._handle_error("First command was not successful")
            
        except Exception as e:
            return self._handle_error(f"Error processing command: {str(e)}")

    def _execute_command(self, command_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a command locally
        
        Args:
            command_name: Name of the command to execute
            parameters: Parameters for the command
            
        Returns:
            Execution result
        """
        try:
            # Get the command instance
            command = self.command_discovery.get_command(command_name)
            
            if not command:
                return self._handle_error(f"Unknown command: {command_name}")
            
            # Validate parameters
            validation_result = self._validate_parameters(command, parameters)
            if not validation_result["success"]:
                return validation_result
            
            # Execute the command
            print(f"🚀 Executing command: {command_name} with params: {parameters}")
            result = command.run(parameters)
            
            # Handle command execution result
            if result.get("success"):
                return {
                    "success": True,
                    "message": result.get("message", f"Successfully executed {command_name}"),
                    "data": result.get("data"),
                    "errors": None
                }
            else:
                return self._handle_command_error(result)
                
        except Exception as e:
            return self._handle_error(f"Error executing command {command_name}: {str(e)}")

    def _validate_parameters(self, command, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate parameters against the command's parameter definitions
        
        Args:
            command: The command instance
            parameters: Parameters to validate
            
        Returns:
            Validation result
        """
        missing_params = []
        invalid_params = []
        
        for param in command.parameters:
            param_name = param.name
            param_value = parameters.get(param_name)
            
            # Check if required parameter is missing
            if param.required and param_value is None:
                missing_params.append({
                    "name": param_name,
                    "description": param.description,
                    "type": param.param_type
                })
                continue
            
            # Validate parameter value
            if param_value is not None:
                is_valid, error_msg = param.validate(param_value)
                if not is_valid:
                    invalid_params.append({
                        "name": param_name,
                        "value": param_value,
                        "error": error_msg,
                        "description": param.description
                    })
        
        if missing_params or invalid_params:
            return {
                "success": False,
                "error_code": "parameter_validation_failed",
                "missing_parameters": missing_params,
                "invalid_parameters": invalid_params,
                "message": "Parameter validation failed"
            }
        
        return {"success": True}

    def _handle_command_error(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle errors returned by command execution"""
        error_code = result.get("error_code", "unknown_error")
        
        return {
            "success": False,
            "error_code": error_code,
            "message": result.get("message", "Command execution failed"),
            "data": result.get("data"),
            "errors": result.get("errors", {})
        }

    def _handle_error(self, message: str, error_code: str = "execution_error") -> Dict[str, Any]:
        """Handle general errors"""
        return {
            "success": False,
            "error_code": error_code,
            "message": message,
            "data": None,
            "errors": {"general": message}
        }

    def speak_result(self, result: Dict[str, Any]):
        """Speak the result of command execution"""
        tts_provider = get_tts_provider()
        
        if result.get("success"):
            message = result.get("message", "Command executed successfully")
        else:
            message = result.get("message", "An error occurred")
            
        tts_provider.speak(False, message) 