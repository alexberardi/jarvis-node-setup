import unittest
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.command_execution_service import CommandExecutionService
from utils.command_discovery_service import CommandDiscoveryService
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter


class MockParameter(IJarvisParameter):
    def __init__(self, name: str, param_type: str, description: str, required: bool = True):
        self._name = name
        self._param_type = param_type
        self._description = description
        self._required = required

    @property
    def name(self) -> str:
        return self._name

    @property
    def param_type(self) -> str:
        return self._param_type

    @property
    def description(self) -> str:
        return self._description

    @property
    def required(self) -> bool:
        return self._required


class MockLightCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "turn_on_lights"

    @property
    def description(self) -> str:
        return "Turn on lights in a room"

    @property
    def keywords(self) -> List[str]:
        return ["turn on", "turn on lights", "lights on", "switch on lights"]

    @property
    def parameters(self) -> list:
        return [MockParameter("room", "str", "Room name")]

    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        room = params.get("room", "unknown")
        return {
            "success": True,
            "message": f"Lights turned on in {room}",
            "data": {"room": room, "status": "on"},
            "errors": None
        }


class TestVoiceCommandPipeline(unittest.TestCase):
    """Integration tests for the complete voice command pipeline"""

    def setUp(self):
        """Set up test fixtures"""
        # Mock config
        with patch('utils.command_execution_service.Config') as mock_config:
            mock_config.get_str.side_effect = lambda key, default=None: {
                "jarvis_command_center": "http://test-command-center.com",
                "node_id": "test-node",
                "room": "test-room"
            }.get(key, default)
            
            self.execution_service = CommandExecutionService()

    @patch('utils.command_execution_service.RestClient')
    def test_complete_voice_command_pipeline(self, mock_rest_client):
        """Test the complete pipeline from voice command to execution"""
        # Mock command center response
        mock_response = {
            "commands": [
                {
                    "success": True,
                    "command_name": "turn_on_lights",
                    "parameters": {"room": "kitchen"},
                    "errors": None
                }
            ]
        }
        mock_rest_client.post.return_value = mock_response
        
        # Mock command discovery
        mock_command = MockLightCommand()
        with patch.object(self.execution_service.command_discovery, 'get_available_commands_schema') as mock_schema:
            mock_schema.return_value = [mock_command.get_command_schema()]
            
            with patch.object(self.execution_service.command_discovery, 'get_command') as mock_get_command:
                mock_get_command.return_value = mock_command
                
                # Test the complete pipeline
                result = self.execution_service.process_voice_command("turn on the lights in the kitchen")
                
                # Verify the result
                self.assertTrue(result["success"])
                self.assertIn("Lights turned on in kitchen", result["message"])
                self.assertEqual(result["data"]["room"], "kitchen")
                self.assertEqual(result["data"]["status"], "on")

    @patch('utils.command_execution_service.RestClient')
    def test_voice_command_with_missing_parameters(self, mock_rest_client):
        """Test voice command that results in missing parameters"""
        # Mock command center response with missing parameters
        mock_response = {
            "commands": [
                {
                    "success": True,
                    "command_name": "turn_on_lights",
                    "parameters": {},  # Missing room parameter
                    "errors": None
                }
            ]
        }
        mock_rest_client.post.return_value = mock_response
        
        # Mock command discovery
        mock_command = MockLightCommand()
        with patch.object(self.execution_service.command_discovery, 'get_available_commands_schema') as mock_schema:
            mock_schema.return_value = [mock_command.get_command_schema()]
            
            with patch.object(self.execution_service.command_discovery, 'get_command') as mock_get_command:
                mock_get_command.return_value = mock_command
                
                # Test the pipeline
                result = self.execution_service.process_voice_command("turn on the lights")
                
                # Should fail due to missing required parameter
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "parameter_validation_failed")
                self.assertEqual(len(result["missing_parameters"]), 1)
                self.assertEqual(result["missing_parameters"][0]["name"], "room")

    @patch('utils.command_execution_service.RestClient')
    def test_voice_command_with_invalid_parameters(self, mock_rest_client):
        """Test voice command with invalid parameter types"""
        # Mock command center response with wrong parameter type
        mock_response = {
            "commands": [
                {
                    "success": True,
                    "command_name": "turn_on_lights",
                    "parameters": {"room": 123},  # Room should be string, not int
                    "errors": None
                }
            ]
        }
        mock_rest_client.post.return_value = mock_response
        
        # Mock command discovery
        mock_command = MockLightCommand()
        with patch.object(self.execution_service.command_discovery, 'get_available_commands_schema') as mock_schema:
            mock_schema.return_value = [mock_command.get_command_schema()]
            
            with patch.object(self.execution_service.command_discovery, 'get_command') as mock_get_command:
                mock_get_command.return_value = mock_command
                
                # Test the pipeline
                result = self.execution_service.process_voice_command("turn on the lights")
                
                # Should fail due to invalid parameter type
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "parameter_validation_failed")
                self.assertEqual(len(result["invalid_parameters"]), 1)
                self.assertEqual(result["invalid_parameters"][0]["name"], "room")

    @patch('utils.command_execution_service.RestClient')
    def test_voice_command_unknown_command(self, mock_rest_client):
        """Test voice command for unknown command"""
        # Mock command center response for unknown command
        mock_response = {
            "commands": [
                {
                    "success": True,
                    "command_name": "unknown_command",
                    "parameters": {},
                    "errors": None
                }
            ]
        }
        mock_rest_client.post.return_value = mock_response
        
        # Mock command discovery
        with patch.object(self.execution_service.command_discovery, 'get_available_commands_schema') as mock_schema:
            mock_schema.return_value = []
            
            with patch.object(self.execution_service.command_discovery, 'get_command') as mock_get_command:
                mock_get_command.return_value = None  # Command not found
                
                # Test the pipeline
                result = self.execution_service.process_voice_command("do something unknown")
                
                # Should fail due to unknown command
                self.assertFalse(result["success"])
                self.assertIn("Unknown command", result["message"])

    @patch('utils.command_execution_service.RestClient')
    def test_voice_command_network_error(self, mock_rest_client):
        """Test voice command when command center is unreachable"""
        # Mock network error
        mock_rest_client.post.return_value = None
        
        # Test the pipeline
        result = self.execution_service.process_voice_command("turn on the lights")
        
        # Should fail due to network error
        self.assertFalse(result["success"])
        self.assertIn("Failed to communicate", result["message"])


class TestCommandDiscoveryIntegration(unittest.TestCase):
    """Integration tests for command discovery"""

    def setUp(self):
        """Set up test fixtures"""
        self.discovery_service = CommandDiscoveryService(refresh_interval=1)
        # Stop the background thread for testing
        self.discovery_service._refresh_thread.join(timeout=0.1)

    def test_command_discovery_and_schema_generation(self):
        """Test that commands are discovered and schemas are generated correctly"""
        # Create mock commands
        mock_command = MockLightCommand()
        
        # Mock the discovery process by directly setting the cache
        self.discovery_service._commands_cache = {"turn_on_lights": mock_command}
        
        # Test command retrieval
        command = self.discovery_service.get_command("turn_on_lights")
        self.assertIsNotNone(command)
        if command:
            self.assertEqual(command.command_name, "turn_on_lights")
        
        # Test schema generation
        schema = self.discovery_service.get_available_commands_schema()
        self.assertEqual(len(schema), 1)
        
        command_schema = schema[0]
        self.assertEqual(command_schema["command_name"], "turn_on_lights")
        self.assertEqual(command_schema["description"], "Turn on lights in a room")
        self.assertEqual(len(command_schema["parameters"]), 1)
        
        # Check parameter schema
        param_schema = command_schema["parameters"][0]
        self.assertEqual(param_schema["name"], "room")
        self.assertEqual(param_schema["type"], "str")
        self.assertTrue(param_schema["required"])


if __name__ == '__main__':
    unittest.main() 