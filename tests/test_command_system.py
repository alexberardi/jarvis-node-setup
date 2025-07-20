import unittest
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter
from utils.command_discovery_service import CommandDiscoveryService
from utils.command_execution_service import CommandExecutionService


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


class MockCommand(IJarvisCommand):
    def __init__(self, command_name: str, description: str, parameters: Optional[List[IJarvisParameter]] = None):
        self._command_name = command_name
        self._description = description
        self._parameters: List[IJarvisParameter] = parameters if parameters is not None else []

    @property
    def command_name(self) -> str:
        return self._command_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return self._parameters

    @property
    def keywords(self) -> List[str]:
        return [self._command_name, self._description.lower()]

    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "message": f"Executed {self.command_name}",
            "data": params,
            "errors": None
        }


class TestCommandDiscoveryService(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.discovery_service = CommandDiscoveryService(refresh_interval=1)
        # Stop the background thread for testing
        self.discovery_service._refresh_thread.join(timeout=0.1)

    def test_discover_commands(self):
        """Test command discovery"""
        # Create mock commands
        mock_command1 = MockCommand("test_command_1", "Test command 1")
        mock_command2 = MockCommand("test_command_2", "Test command 2")
        
        # Mock the discovery process by directly setting the cache
        self.discovery_service._commands_cache = {
            "test_command_1": mock_command1,
            "test_command_2": mock_command2
        }
        
        commands = self.discovery_service.get_all_commands()
        self.assertEqual(len(commands), 2)
        self.assertIn("test_command_1", commands)
        self.assertIn("test_command_2", commands)

    def test_get_command(self):
        """Test getting specific command"""
        mock_command = MockCommand("test_command", "Test command")
        
        # Mock by directly setting the cache
        self.discovery_service._commands_cache = {"test_command": mock_command}
        
        command = self.discovery_service.get_command("test_command")
        self.assertIsNotNone(command)
        if command:
            self.assertEqual(command.command_name, "test_command")
        
        # Test non-existent command
        self.assertIsNone(self.discovery_service.get_command("non_existent"))

    def test_get_available_commands_schema(self):
        """Test schema generation"""
        param1 = MockParameter("param1", "str", "Test parameter 1")
        param2 = MockParameter("param2", "int", "Test parameter 2", required=False)
        
        mock_command = MockCommand("test_command", "Test command", [param1, param2])
        
        # Mock by directly setting the cache
        self.discovery_service._commands_cache = {"test_command": mock_command}
        
        schema = self.discovery_service.get_available_commands_schema()
        self.assertEqual(len(schema), 1)
        
        command_schema = schema[0]
        self.assertEqual(command_schema["command_name"], "test_command")
        self.assertEqual(command_schema["description"], "Test command")
        self.assertEqual(len(command_schema["parameters"]), 2)
        
        # Check parameter schema
        param_schema = command_schema["parameters"][0]
        self.assertEqual(param_schema["name"], "param1")
        self.assertEqual(param_schema["type"], "str")
        self.assertTrue(param_schema["required"])


class TestCommandExecutionService(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        with patch('utils.command_execution_service.Config') as mock_config:
            mock_config.get_str.side_effect = lambda key, default=None: {
                "jarvis_command_center": "http://test-command-center.com",
                "node_id": "test-node",
                "room": "test-room"
            }.get(key, default)
            
            self.execution_service = CommandExecutionService()

    @patch('utils.command_execution_service.RestClient')
    def test_process_voice_command_success(self, mock_rest_client):
        """Test successful command processing"""
        # Mock command center response
        mock_response = {
            "commands": [
                {
                    "success": True,
                    "command_name": "test_command",
                    "parameters": {"param1": "value1"},
                    "errors": None
                }
            ]
        }
        mock_rest_client.post.return_value = mock_response
        
        # Mock command discovery
        mock_command = MockCommand("test_command", "Test command")
        with patch.object(self.execution_service.command_discovery, 'get_available_commands_schema') as mock_schema:
            mock_schema.return_value = [mock_command.get_command_schema()]
            
            with patch.object(self.execution_service.command_discovery, 'get_command') as mock_get_command:
                mock_get_command.return_value = mock_command
                
                result = self.execution_service.process_voice_command("test voice command")
                
                self.assertTrue(result["success"])
                self.assertIn("test_command", result["message"])

    @patch('utils.command_execution_service.RestClient')
    def test_process_voice_command_no_response(self, mock_rest_client):
        """Test command processing with no response from command center"""
        mock_rest_client.post.return_value = None
        
        result = self.execution_service.process_voice_command("test voice command")
        
        self.assertFalse(result["success"])
        self.assertIn("Failed to communicate", result["message"])

    @patch('utils.command_execution_service.RestClient')
    def test_process_voice_command_no_command_name(self, mock_rest_client):
        """Test command processing with missing command name in response"""
        mock_response = {"parameters": {"param1": "value1"}}
        mock_rest_client.post.return_value = mock_response
        
        result = self.execution_service.process_voice_command("test voice command")
        
        self.assertFalse(result["success"])
        self.assertIn("No commands specified in response", result["message"])

    def test_validate_parameters_success(self):
        """Test parameter validation success"""
        param1 = MockParameter("param1", "str", "Test parameter 1")
        param2 = MockParameter("param2", "int", "Test parameter 2", required=False)
        
        mock_command = MockCommand("test_command", "Test command", [param1, param2])
        
        params = {"param1": "value1", "param2": 42}
        result = self.execution_service._validate_parameters(mock_command, params)
        
        self.assertTrue(result["success"])

    def test_validate_parameters_missing_required(self):
        """Test parameter validation with missing required parameter"""
        param1 = MockParameter("param1", "str", "Test parameter 1", required=True)
        mock_command = MockCommand("test_command", "Test command", [param1])
        
        params = {}  # Missing required parameter
        result = self.execution_service._validate_parameters(mock_command, params)
        
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "parameter_validation_failed")
        self.assertEqual(len(result["missing_parameters"]), 1)
        self.assertEqual(result["missing_parameters"][0]["name"], "param1")

    def test_validate_parameters_invalid_type(self):
        """Test parameter validation with invalid parameter type"""
        param1 = MockParameter("param1", "int", "Test parameter 1")
        mock_command = MockCommand("test_command", "Test command", [param1])
        
        params = {"param1": "not_an_integer"}
        result = self.execution_service._validate_parameters(mock_command, params)
        
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "parameter_validation_failed")
        self.assertEqual(len(result["invalid_parameters"]), 1)
        self.assertEqual(result["invalid_parameters"][0]["name"], "param1")

    @patch('utils.command_execution_service.get_tts_provider')
    def test_speak_result(self, mock_get_tts):
        """Test speaking command results"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Test success result
        success_result = {
            "success": True,
            "message": "Command executed successfully"
        }
        self.execution_service.speak_result(success_result)
        mock_tts.speak.assert_called_once_with(False, "Command executed successfully")
        
        # Test error result
        error_result = {
            "success": False,
            "message": "An error occurred"
        }
        self.execution_service.speak_result(error_result)
        mock_tts.speak.assert_called_with(False, "An error occurred")


if __name__ == '__main__':
    unittest.main() 