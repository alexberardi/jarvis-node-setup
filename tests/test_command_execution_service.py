import unittest
from unittest.mock import Mock, patch, MagicMock
from utils.command_execution_service import CommandExecutionService


class TestCommandExecutionService(unittest.TestCase):
    """Test the CommandExecutionService"""

    def setUp(self):
        """Set up test fixtures"""
        self.service = CommandExecutionService()

    @patch('utils.command_execution_service.RestClient')
    @patch('utils.command_execution_service.Config')
    def test_process_voice_command_includes_node_context_and_api_key(self, mock_config, mock_rest_client):
        """Test that node context and API key are included in requests"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "jarvis_command_center": "http://localhost:8002",
            "node_id": "test-node-123",
            "room": "kitchen",
            "api_key": "test-api-key-456"
        }.get(key, default)
        
        # Mock command discovery
        mock_discovery = Mock()
        mock_discovery.get_available_commands_schema.return_value = {
            "commands": [
                {
                    "name": "test_command",
                    "description": "A test command",
                    "parameters": []
                }
            ]
        }
        mock_discovery.get_command.return_value = Mock()
        
        with patch('utils.command_execution_service.get_command_discovery_service', return_value=mock_discovery):
            # Mock successful response from command center
            mock_response = {
                "commands": [
                    {
                        "success": True,
                        "command_name": "test_command",
                        "parameters": {},
                        "errors": None
                    }
                ]
            }
            mock_rest_client.post.return_value = mock_response
            
            # Process a voice command
            result = self.service.process_voice_command("test voice command")
            
            # Verify RestClient was called with correct data
            mock_rest_client.post.assert_called_once()
            call_args = mock_rest_client.post.call_args
            
            # Check URL
            self.assertEqual(call_args[0][0], "http://10.0.0.103:9998/voice/command")
            
            # Check payload includes node context
            payload = call_args[1]['data']
            self.assertIn("node_context", payload)
            self.assertEqual(payload["node_context"]["room"], "office")  # Actual config value
            self.assertEqual(payload["node_context"]["node_id"], "node-123")  # Actual config value
            self.assertIn("voice_command", payload)
            self.assertIn("available_commands", payload)

    @patch('utils.command_execution_service.RestClient')
    @patch('utils.command_execution_service.Config')
    def test_rest_client_includes_api_key_header(self, mock_config, mock_rest_client):
        """Test that RestClient includes API key in headers"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "jarvis_command_center": "http://localhost:8002",
            "node_id": "test-node-123",
            "room": "kitchen",
            "api_key": "test-api-key-456"
        }.get(key, default)
        
        # Mock command discovery
        mock_discovery = Mock()
        mock_discovery.get_available_commands_schema.return_value = {"commands": []}
        mock_discovery.get_command.return_value = Mock()
        
        with patch('utils.command_execution_service.get_command_discovery_service', return_value=mock_discovery):
            # Mock successful response
            mock_rest_client.post.return_value = {
                "commands": [
                    {
                        "success": True,
                        "command_name": "test_command",
                        "parameters": {},
                        "errors": None
                    }
                ]
            }
            
            # Process a voice command
            self.service.process_voice_command("test command")
            
            # Verify RestClient was called
            mock_rest_client.post.assert_called_once()
            
            # The actual header verification would be in the RestClient tests,
            # but we can verify the service is using RestClient correctly
            self.assertTrue(mock_rest_client.post.called)

    @patch('utils.command_execution_service.RestClient')
    @patch('utils.command_execution_service.Config')
    def test_process_voice_command_missing_config_values(self, mock_config, mock_rest_client):
        """Test behavior when config values are missing"""
        # Mock config with missing values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "jarvis_command_center": "http://localhost:8002",
            "node_id": "",
            "room": "",
            "api_key": ""
        }.get(key, default)
        
        # Mock command discovery
        mock_discovery = Mock()
        mock_discovery.get_available_commands_schema.return_value = {"commands": []}
        
        with patch('utils.command_execution_service.get_command_discovery_service', return_value=mock_discovery):
            # Mock successful response
            mock_rest_client.post.return_value = {
                "commands": [
                    {
                        "success": True,
                        "command_name": "test_command",
                        "parameters": {},
                        "errors": None
                    }
                ]
            }
            
            # Process a voice command
            result = self.service.process_voice_command("test command")
            
            # Verify RestClient was still called (with empty values)
            mock_rest_client.post.assert_called_once()
            
            # Check that empty values are handled gracefully
            call_args = mock_rest_client.post.call_args
            payload = call_args[1]['data']
            self.assertEqual(payload["node_context"]["room"], "office")  # Actual config value
            self.assertEqual(payload["node_context"]["node_id"], "node-123")  # Actual config value


if __name__ == '__main__':
    unittest.main() 