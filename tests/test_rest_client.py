import unittest
from unittest.mock import Mock, patch, MagicMock
import requests
from clients.rest_client import RestClient


class TestRestClient(unittest.TestCase):
    """Test the RestClient"""

    @patch('clients.rest_client.requests.post')
    @patch('clients.rest_client.Config')
    def test_post_includes_api_key_and_node_id_headers(self, mock_config, mock_requests_post):
        """Test that POST requests include API key and node ID headers"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "node_id": "test-node-123",
            "api_key": "test-api-key-456"
        }.get(key, default)
        
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {"status": "success"}
        mock_requests_post.return_value = mock_response
        
        # Make a POST request
        result = RestClient.post("http://test.com/api", {"test": "data"})
        
        # Verify request was made with correct headers
        mock_requests_post.assert_called_once()
        call_args = mock_requests_post.call_args
        
        # Check headers
        headers = call_args[1]['headers']
        self.assertEqual(headers["X-Node-ID"], "test-node-123")
        self.assertEqual(headers["X-API-Key"], "test-api-key-456")
        
        # Check other parameters
        self.assertEqual(call_args[0][0], "http://test.com/api")
        self.assertEqual(call_args[1]['json'], {"test": "data"})

    @patch('clients.rest_client.requests.post')
    @patch('clients.rest_client.Config')
    def test_post_with_missing_config_values(self, mock_config, mock_requests_post):
        """Test behavior when config values are missing"""
        # Mock config with missing values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "node_id": "",
            "api_key": ""
        }.get(key, default)
        
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {"status": "success"}
        mock_requests_post.return_value = mock_response
        
        # Make a POST request
        result = RestClient.post("http://test.com/api", {"test": "data"})
        
        # Verify request was made with empty headers
        mock_requests_post.assert_called_once()
        call_args = mock_requests_post.call_args
        
        # Check headers are empty strings (not None)
        headers = call_args[1]['headers']
        self.assertEqual(headers["X-Node-ID"], "")
        self.assertEqual(headers["X-API-Key"], "")

    @patch('clients.rest_client.requests.post')
    @patch('clients.rest_client.Config')
    def test_post_with_files(self, mock_config, mock_requests_post):
        """Test POST request with files parameter"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "node_id": "test-node",
            "api_key": "test-key"
        }.get(key, default)
        
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {"status": "success"}
        mock_requests_post.return_value = mock_response
        
        # Mock file data
        files = {"file": ("test.txt", "test content")}
        
        # Make a POST request with files
        result = RestClient.post("http://test.com/upload", files=files)
        
        # Verify request was made with files
        mock_requests_post.assert_called_once()
        call_args = mock_requests_post.call_args
        
        # Check files parameter
        self.assertEqual(call_args[1]['files'], files)
        
        # Check headers are still included
        headers = call_args[1]['headers']
        self.assertEqual(headers["X-Node-ID"], "test-node")
        self.assertEqual(headers["X-API-Key"], "test-key")

    @patch('clients.rest_client.requests.post')
    @patch('clients.rest_client.Config')
    def test_post_request_exception_handling(self, mock_config, mock_requests_post):
        """Test handling of request exceptions"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "node_id": "test-node",
            "api_key": "test-key"
        }.get(key, default)
        
        # Mock request exception
        mock_requests_post.side_effect = requests.RequestException("Network error")
        
        # Make a POST request
        result = RestClient.post("http://test.com/api", {"test": "data"})
        
        # Verify None is returned on error
        self.assertIsNone(result)
        
        # Verify request was attempted
        mock_requests_post.assert_called_once()

    @patch('clients.rest_client.requests.post')
    @patch('clients.rest_client.Config')
    def test_post_with_custom_timeout(self, mock_config, mock_requests_post):
        """Test POST request with custom timeout"""
        # Mock config values
        mock_config.get_str.side_effect = lambda key, default=None: {
            "node_id": "test-node",
            "api_key": "test-key"
        }.get(key, default)
        
        # Mock successful response
        mock_response = Mock()
        mock_response.json.return_value = {"status": "success"}
        mock_requests_post.return_value = mock_response
        
        # Make a POST request with custom timeout
        result = RestClient.post("http://test.com/api", {"test": "data"}, timeout=60)
        
        # Verify request was made with custom timeout
        mock_requests_post.assert_called_once()
        call_args = mock_requests_post.call_args
        
        # Check timeout
        self.assertEqual(call_args[1]['timeout'], 60)


if __name__ == '__main__':
    unittest.main() 