import unittest
import tempfile
import json
import os
from unittest.mock import patch
from utils.config_service import Config


class TestConfigService(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        # Create a temporary config file for testing
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.json")
        
        # Sample config data
        self.test_config = {
            "node_id": "test-node-123",
            "room": "kitchen",
            "mqtt_port": 1884,
            "mic_sample_rate": 48000,
            "music_assistant_enabled": True,
            "volume": 0.75,
            "api_key": "test-api-key-123",
            "empty_string": "",
            "zero_value": 0,
            "false_value": False
        }
        
        # Write test config to temp file
        with open(self.config_path, 'w') as f:
            json.dump(self.test_config, f)

    def tearDown(self):
        """Clean up test fixtures"""
        if os.path.exists(self.config_path):
            os.remove(self.config_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    @patch('utils.config_service.os.path.expanduser')
    def test_get_str(self, mock_expanduser):
        """Test string config getter"""
        mock_expanduser.return_value = self.config_path
        
        # Test existing string values
        self.assertEqual(Config.get_str("node_id"), "test-node-123")
        self.assertEqual(Config.get_str("room"), "kitchen")
        self.assertEqual(Config.get_str("api_key"), "test-api-key-123")
        
        # Test empty string
        self.assertEqual(Config.get_str("empty_string"), "")
        
        # Test non-string values (should convert)
        self.assertEqual(Config.get_str("mqtt_port"), "1884")
        self.assertEqual(Config.get_str("music_assistant_enabled"), "True")
        
        # Test missing key with default
        self.assertEqual(Config.get_str("missing_key", "default"), "default")
        self.assertIsNone(Config.get_str("missing_key"))

    @patch('utils.config_service.os.path.expanduser')
    def test_get_int(self, mock_expanduser):
        """Test integer config getter"""
        mock_expanduser.return_value = self.config_path
        
        # Test existing integer values
        self.assertEqual(Config.get_int("mqtt_port", 0), 1884)
        self.assertEqual(Config.get_int("mic_sample_rate", 0), 48000)
        self.assertEqual(Config.get_int("zero_value", 0), 0)
        
        # Test string values that can be converted
        self.assertEqual(Config.get_int("node_id", 0), 0)  # Can't convert "test-node-123"
        
        # Test missing key with default
        self.assertEqual(Config.get_int("missing_key", 42), 42)

    @patch('utils.config_service.os.path.expanduser')
    def test_get_bool(self, mock_expanduser):
        """Test boolean config getter"""
        mock_expanduser.return_value = self.config_path
        
        # Test existing boolean values
        self.assertTrue(Config.get_bool("music_assistant_enabled"))
        self.assertFalse(Config.get_bool("false_value"))
        
        # Test string values that can be converted
        self.assertTrue(Config.get_bool("node_id"))  # Non-empty string is True
        self.assertFalse(Config.get_bool("empty_string"))  # Empty string is False
        
        # Test integer values
        self.assertTrue(Config.get_bool("mqtt_port"))  # Non-zero is True
        self.assertFalse(Config.get_bool("zero_value"))  # Zero is False
        
        # Test missing key with default
        self.assertTrue(Config.get_bool("missing_key", True))
        self.assertFalse(Config.get_bool("missing_key", False))
        self.assertIsNone(Config.get_bool("missing_key"))

    @patch('utils.config_service.os.path.expanduser')
    def test_get_float(self, mock_expanduser):
        """Test float config getter"""
        mock_expanduser.return_value = self.config_path
        
        # Test existing float values
        self.assertEqual(Config.get_float("volume", 0.0), 0.75)
        
        # Test integer values that can be converted
        self.assertEqual(Config.get_float("mqtt_port", 0.0), 1884.0)
        
        # Test string values that can be converted
        self.assertEqual(Config.get_float("node_id", 0.0), 0.0)  # Can't convert "test-node-123"
        
        # Test missing key with default
        self.assertEqual(Config.get_float("missing_key", 3.14), 3.14)

    @patch('utils.config_service.os.path.expanduser')
    def test_legacy_get(self, mock_expanduser):
        """Test legacy get method for backward compatibility"""
        mock_expanduser.return_value = self.config_path
        
        # Should work like get_str
        self.assertEqual(Config.get("node_id"), "test-node-123")
        self.assertEqual(Config.get("missing_key", "default"), "default")
        self.assertIsNone(Config.get("missing_key"))

    @patch('utils.config_service.os.path.expanduser')
    def test_config_file_not_found(self, mock_expanduser):
        """Test behavior when config file doesn't exist"""
        mock_expanduser.return_value = "/nonexistent/config.json"
        
        # Should return defaults when file doesn't exist
        self.assertEqual(Config.get_str("any_key", "default"), "default")
        self.assertEqual(Config.get_int("any_key", 42), 42)
        self.assertTrue(Config.get_bool("any_key", True))
        self.assertEqual(Config.get_float("any_key", 3.14), 3.14)

    @patch('utils.config_service.os.path.expanduser')
    def test_invalid_json(self, mock_expanduser):
        """Test behavior with invalid JSON"""
        # Create invalid JSON file
        invalid_config_path = os.path.join(self.temp_dir, "invalid.json")
        with open(invalid_config_path, 'w') as f:
            f.write("invalid json content")
        
        mock_expanduser.return_value = invalid_config_path
        
        # Should handle gracefully and return defaults
        self.assertEqual(Config.get_str("any_key", "default"), "default")
        
        # Clean up
        os.remove(invalid_config_path)


if __name__ == '__main__':
    unittest.main() 