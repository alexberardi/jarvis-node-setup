import unittest
import json
import sys
import os
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.mqtt_tts_listener import on_message, handle_tts
from tts_providers.jarvis_tts_api import JarvisTTS
from tts_providers.espeak import EspeakTTS


class TestMQTTTTSIntegration(unittest.TestCase):
    """Integration tests for MQTT to TTS flow"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_client = Mock()
        self.mock_userdata = None

    def create_mqtt_message(self, payload: list) -> Mock:
        """Helper to create a mock MQTT message"""
        mock_msg = Mock()
        mock_msg.payload = json.dumps(payload).encode()
        return mock_msg

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_complete_mqtt_tts_flow(self, mock_get_tts):
        """Test complete flow from MQTT message to TTS execution"""
        # Mock TTS provider
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Create MQTT message
        payload = [{"command": "tts", "details": {"message": "Integration test message"}}]
        mock_msg = self.create_mqtt_message(payload)
        
        # Process message
        on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify TTS was called correctly
        mock_tts.speak.assert_called_once_with(True, "Integration test message")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_mqtt_with_jarvis_tts_api(self, mock_get_tts):
        """Test MQTT flow specifically with JarvisTTS provider"""
        # Mock JarvisTTS
        mock_jarvis_tts = Mock(spec=JarvisTTS)
        mock_get_tts.return_value = mock_jarvis_tts
        
        # Create MQTT message
        payload = [{"command": "tts", "details": {"message": "Jarvis TTS test"}}]
        mock_msg = self.create_mqtt_message(payload)
        
        # Process message
        on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify JarvisTTS was called
        mock_jarvis_tts.speak.assert_called_once_with(True, "Jarvis TTS test")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_mqtt_with_espeak(self, mock_get_tts):
        """Test MQTT flow specifically with EspeakTTS provider"""
        # Mock EspeakTTS
        mock_espeak_tts = Mock(spec=EspeakTTS)
        mock_get_tts.return_value = mock_espeak_tts
        
        # Create MQTT message
        payload = [{"command": "tts", "details": {"message": "Espeak test"}}]
        mock_msg = self.create_mqtt_message(payload)
        
        # Process message
        on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify EspeakTTS was called
        mock_espeak_tts.speak.assert_called_once_with(True, "Espeak test")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_multiple_mqtt_messages(self, mock_get_tts):
        """Test processing multiple MQTT messages in sequence"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Process multiple messages
        messages = [
            [{"command": "tts", "details": {"message": "First message"}}],
            [{"command": "tts", "details": {"message": "Second message"}}],
            [{"command": "tts", "details": {"message": "Third message"}}]
        ]
        
        for payload in messages:
            mock_msg = self.create_mqtt_message(payload)
            on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify all messages were processed
        expected_calls = [
            ((True, "First message"),),
            ((True, "Second message"),),
            ((True, "Third message"),)
        ]
        mock_tts.speak.assert_has_calls(expected_calls)
        self.assertEqual(mock_tts.speak.call_count, 3)

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_mqtt_message_with_special_characters(self, mock_get_tts):
        """Test MQTT messages with special characters and unicode"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Test various special characters
        test_messages = [
            "Hello, world!",
            "Temperature is 23.5°C",
            "Alert: System status = OK",
            "Message with 'quotes' and \"double quotes\"",
            "Unicode: café, naïve, résumé"
        ]
        
        for message in test_messages:
            payload = [{"command": "tts", "details": {"message": message}}]
            mock_msg = self.create_mqtt_message(payload)
            on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify all messages were processed correctly
        self.assertEqual(mock_tts.speak.call_count, len(test_messages))
        for i, message in enumerate(test_messages):
            call_args = mock_tts.speak.call_args_list[i]
            self.assertEqual(call_args[0][1], message)

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_mqtt_error_recovery(self, mock_get_tts):
        """Test that MQTT processing continues after TTS errors"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # First message fails, second succeeds
        mock_tts.speak.side_effect = [Exception("TTS Error"), None]
        
        messages = [
            [{"command": "tts", "details": {"message": "Failing message"}}],
            [{"command": "tts", "details": {"message": "Working message"}}]
        ]
        
        for payload in messages:
            mock_msg = self.create_mqtt_message(payload)
            # Should not raise exception
            on_message(self.mock_client, self.mock_userdata, mock_msg)
        
        # Verify both were attempted
        self.assertEqual(mock_tts.speak.call_count, 2)

    def test_mqtt_message_format_validation(self):
        """Test various MQTT message formats and validation"""
        test_cases = [
            # Valid format
            ([{"command": "tts", "details": {"message": "Valid"}}], True),
            # Missing command
            ([{"details": {"message": "No command"}}], False),
            # Missing details
            ([{"command": "tts"}], True),  # Should use empty details
            # Empty message
            ([{"command": "tts", "details": {"message": ""}}], True),
            # Multiple commands
            ([
                {"command": "tts", "details": {"message": "First"}},
                {"command": "tts", "details": {"message": "Second"}}
            ], True),
            # Unknown command
            ([{"command": "unknown", "details": {}}], False),
        ]
        
        for payload, should_call_tts in test_cases:
            with self.subTest(payload=payload):
                with patch('scripts.mqtt_tts_listener.get_tts_provider') as mock_get_tts:
                    mock_tts = Mock()
                    mock_get_tts.return_value = mock_tts
                    
                    mock_msg = self.create_mqtt_message(payload)
                    on_message(self.mock_client, self.mock_userdata, mock_msg)
                    
                    if should_call_tts:
                        mock_tts.speak.assert_called()
                    else:
                        mock_tts.speak.assert_not_called()


class TestMQTTRealWorldScenarios(unittest.TestCase):
    """Test real-world MQTT usage scenarios"""

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_home_assistant_integration_format(self, mock_get_tts):
        """Test MQTT message format that might come from Home Assistant"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Simulate Home Assistant style message
        payload = [{
            "command": "tts",
            "details": {
                "message": "The temperature in the living room is 22 degrees Celsius",
                "priority": "normal",
                "room": "living_room"
            }
        }]
        
        mock_msg = Mock()
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(Mock(), None, mock_msg)
        
        # Should extract just the message
        mock_tts.speak.assert_called_once_with(True, "The temperature in the living room is 22 degrees Celsius")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_alert_notification_format(self, mock_get_tts):
        """Test MQTT message format for alert notifications"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Simulate alert notification
        payload = [{
            "command": "tts",
            "details": {
                "message": "🚨 ALERT: Motion detected in the backyard",
                "urgency": "high",
                "source": "security_camera"
            }
        }]
        
        mock_msg = Mock()
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(Mock(), None, mock_msg)
        
        # Should handle emoji and special characters
        mock_tts.speak.assert_called_once_with(True, "🚨 ALERT: Motion detected in the backyard")

    @patch('scripts.mqtt_tts_listener.get_tts_provider')
    def test_weather_update_format(self, mock_get_tts):
        """Test MQTT message format for weather updates"""
        mock_tts = Mock()
        mock_get_tts.return_value = mock_tts
        
        # Simulate weather update
        payload = [{
            "command": "tts",
            "details": {
                "message": "Today's forecast: Sunny with a high of 75°F and low of 58°F",
                "type": "weather",
                "location": "home"
            }
        }]
        
        mock_msg = Mock()
        mock_msg.payload = json.dumps(payload).encode()
        
        on_message(Mock(), None, mock_msg)
        
        # Should handle temperature symbols and formatting
        mock_tts.speak.assert_called_once_with(True, "Today's forecast: Sunny with a high of 75°F and low of 58°F")


if __name__ == '__main__':
    unittest.main() 