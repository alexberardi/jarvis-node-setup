import unittest
import json
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mqtt_tts_listener import (
    handle_tts, 
    command_handlers, 
    on_connect, 
    on_message, 
    start_mqtt_listener
)


class TestMQTTTTSListener(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.mock_tts_provider = Mock()
        self.mock_ma_service = Mock()

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_handle_tts_success(self, mock_get_tts):
        """Test successful TTS handling"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        details = {"message": "Hello world"}
        handle_tts(details)
        
        # Verify TTS provider was called correctly
        self.mock_tts_provider.speak.assert_called_once_with(True, "Hello world")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_handle_tts_empty_message(self, mock_get_tts):
        """Test TTS handling with empty message"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        details = {"message": ""}
        handle_tts(details)
        
        # Should still call speak with empty message
        self.mock_tts_provider.speak.assert_called_once_with(True, "")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_handle_tts_missing_message(self, mock_get_tts):
        """Test TTS handling with missing message"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        details = {}
        handle_tts(details)
        
        # Should use empty string as default
        self.mock_tts_provider.speak.assert_called_once_with(True, "")

    def test_command_handlers_registration(self):
        """Test that TTS command is properly registered"""
        self.assertIn("tts", command_handlers)
        self.assertEqual(command_handlers["tts"], handle_tts)

    def test_on_connect(self):
        """Test MQTT connection callback"""
        mock_client = Mock()
        mock_userdata = None
        mock_flags = {"session present": 0}
        mock_rc = 0
        
        on_connect(mock_client, mock_userdata, mock_flags, mock_rc)
        
        # Verify subscription was called
        mock_client.subscribe.assert_called_once()

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_on_message_valid_tts_command(self, mock_get_tts):
        """Test processing valid TTS command via MQTT"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Create valid MQTT payload
        payload = [{"command": "tts", "details": {"message": "Test message"}}]
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify TTS was called
        self.mock_tts_provider.speak.assert_called_once_with(True, "Test message")

    def test_on_message_invalid_json(self):
        """Test handling invalid JSON payload"""
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        mock_msg.payload = b"invalid json"
        
        # Should not raise exception
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify no TTS calls were made
        self.mock_tts_provider.speak.assert_not_called()

    def test_on_message_not_list(self):
        """Test handling payload that's not a list"""
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Single command object instead of list
        payload = {"command": "tts", "details": {"message": "Test"}}
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify no TTS calls were made
        self.mock_tts_provider.speak.assert_not_called()

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_on_message_unknown_command(self, mock_get_tts):
        """Test handling unknown command"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Unknown command
        payload = [{"command": "unknown_command", "details": {}}]
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify no TTS calls were made
        self.mock_tts_provider.speak.assert_not_called()

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_on_message_multiple_commands(self, mock_get_tts):
        """Test processing multiple commands in one message"""
        mock_get_tts.return_value = self.mock_tts_provider
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Multiple commands
        payload = [
            {"command": "tts", "details": {"message": "First message"}},
            {"command": "tts", "details": {"message": "Second message"}}
        ]
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify both TTS calls were made
        expected_calls = [
            ((True, "First message"),),
            ((True, "Second message"),)
        ]
        self.mock_tts_provider.speak.assert_has_calls(expected_calls)
        self.assertEqual(self.mock_tts_provider.speak.call_count, 2)

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_on_message_tts_exception_handling(self, mock_get_tts):
        """Test handling TTS exceptions gracefully"""
        mock_get_tts.return_value = self.mock_tts_provider
        self.mock_tts_provider.speak.side_effect = Exception("TTS Error")
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        payload = [{"command": "tts", "details": {"message": "Test message"}}]
        mock_msg.payload = json.dumps(payload).encode()
        
        # Should not raise exception
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Verify TTS was attempted
        self.mock_tts_provider.speak.assert_called_once()

    @patch('scripts.mqtt_tts_listener.mqtt.Client')
    @patch('scripts.mqtt_tts_listener.Config')
    def test_start_mqtt_listener_with_auth(self, mock_config, mock_mqtt_client):
        """Test MQTT listener startup with authentication"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "mqtt_topic": "test/topic/#",
            "mqtt_broker": "test.broker.com",
            "mqtt_username": "testuser",
            "mqtt_password": "testpass"
        }.get(key, default)
        mock_config.get_int.return_value = 1883
        
        mock_client_instance = Mock()
        mock_mqtt_client.return_value = mock_client_instance
        
        start_mqtt_listener(self.mock_ma_service)
        
        # Verify client was created and configured
        mock_mqtt_client.assert_called_once()
        mock_client_instance.username_pw_set.assert_called_once_with("testuser", "testpass")
        mock_client_instance.on_connect = on_connect
        mock_client_instance.on_message = on_message
        mock_client_instance.connect.assert_called_once()
        mock_client_instance.loop_forever.assert_called_once()

    @patch('scripts.mqtt_tts_listener.mqtt.Client')
    @patch('scripts.mqtt_tts_listener.Config')
    def test_start_mqtt_listener_without_auth(self, mock_config, mock_mqtt_client):
        """Test MQTT listener startup without authentication"""
        # Mock config values without auth
        mock_config.get_str.side_effect = lambda key, default=None: {
            "mqtt_topic": "test/topic/#",
            "mqtt_broker": "test.broker.com",
            "mqtt_username": "",
            "mqtt_password": ""
        }.get(key, default)
        mock_config.get_int.return_value = 1883
        
        mock_client_instance = Mock()
        mock_mqtt_client.return_value = mock_client_instance
        
        start_mqtt_listener(self.mock_ma_service)
        
        # Verify client was created but auth not set
        mock_mqtt_client.assert_called_once()
        mock_client_instance.username_pw_set.assert_not_called()
        mock_client_instance.connect.assert_called_once()
        mock_client_instance.loop_forever.assert_called_once()


class TestMQTTMessageFormats(unittest.TestCase):
    """Test various MQTT message formats and edge cases"""
    
    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_missing_command_field(self, mock_get_tts):
        """Test handling message with missing command field"""
        mock_get_tts.return_value = Mock()
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Missing command field
        payload = [{"details": {"message": "Test"}}]
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Should handle gracefully
        mock_get_tts.return_value.speak.assert_not_called()

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_missing_details_field(self, mock_get_tts):
        """Test handling message with missing details field"""
        mock_get_tts.return_value = Mock()
        
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        # Missing details field
        payload = [{"command": "tts"}]
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(mock_client, mock_userdata, mock_msg)
        
        # Should use empty details
        mock_get_tts.return_value.speak.assert_called_once_with(True, "")

    def test_empty_payload_list(self):
        """Test handling empty payload list"""
        mock_client = Mock()
        mock_userdata = None
        mock_msg = Mock()
        
        payload = []
        mock_msg.payload = json.dumps(payload).encode()
        
        # Should handle gracefully
        on_message(mock_client, mock_userdata, mock_msg)


if __name__ == '__main__':
    unittest.main() 