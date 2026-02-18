# Jarvis Node Setup - Testing Framework

This directory contains a comprehensive test suite for the Jarvis Node Setup project, designed to help you develop with confidence and catch issues early.

## 🧪 Test Structure

### Test Files

- **`test_config_service.py`** - Tests for the configuration management system
- **`test_command_system.py`** - Tests for command discovery and execution
- **`test_providers.py`** - Tests for STT and TTS providers
- **`test_integration.py`** - Integration tests for the complete voice command pipeline
- **`test_mqtt_tts.py`** - Tests for MQTT TTS listener functionality
- **`test_mqtt_integration.py`** - Integration tests for MQTT to TTS flow
- **`test_wake_response_providers.py`** - Tests for wake response provider system

### Test Categories

1. **Unit Tests** - Test individual components in isolation
2. **Integration Tests** - Test how components work together
3. **Mock Tests** - Test external dependencies using mocks
4. **Error Handling Tests** - Test edge cases and error conditions
5. **MQTT Tests** - Test MQTT message processing and TTS integration
6. **Wake Response Tests** - Test dynamic wake response generation

## 🚀 Running Tests

### Prerequisites

Make sure you have the required dependencies installed:

```bash
# On the Pi Zero
cd ~/projects/jarvis-node-setup
source venv/bin/activate
pip install httpx requests
```

### Test Runner

Use the main test runner script from the project root:

```bash
# Run all tests
python3 run_tests.py

# List all available tests
python3 run_tests.py --list

# Run a specific test file
python3 run_tests.py --test test_config_service.py

# Run with verbose output
python3 run_tests.py --verbose
```

### Individual Test Files

You can also run individual test files directly:

```bash
# Run specific test file
python3 -m unittest tests.test_config_service

# Run specific test case
python3 -m unittest tests.test_config_service.TestConfigService.test_get_str

# Run with coverage (if coverage is installed)
python3 -m coverage run -m unittest discover tests
python3 -m coverage report
```

## 📋 Test Coverage

### Configuration Service (`test_config_service.py`)

- ✅ Type-specific getters (`get_str`, `get_int`, `get_bool`, `get_float`)
- ✅ Default value handling
- ✅ Error handling (missing files, invalid JSON)
- ✅ Legacy compatibility
- ✅ Type conversion edge cases

### Command System (`test_command_system.py`)

- ✅ Command discovery and caching
- ✅ Parameter validation
- ✅ Command execution flow
- ✅ Error handling for missing/invalid parameters
- ✅ Schema generation for LLM

### Providers (`test_providers.py`)

- ✅ STT provider (Jarvis Whisper Client)
- ✅ TTS providers (Espeak, Jarvis TTS API)
- ✅ Provider helper functions
- ✅ Error handling for network issues
- ✅ Audio file handling

### Integration (`test_integration.py`)

- ✅ Complete voice command pipeline
- ✅ Command center integration
- ✅ Local command execution
- ✅ Error recovery scenarios
- ✅ Network failure handling

### MQTT TTS (`test_mqtt_tts.py`)

- ✅ MQTT message parsing and validation
- ✅ TTS command handling
- ✅ Error handling for invalid JSON
- ✅ Multiple command processing
- ✅ Authentication handling
- ✅ Connection management

### MQTT Integration (`test_mqtt_integration.py`)

- ✅ Complete MQTT to TTS flow
- ✅ Real-world message formats (Home Assistant, alerts, weather)
- ✅ Special character and unicode handling
- ✅ Error recovery and resilience
- ✅ Multiple provider support (JarvisTTS, EspeakTTS)

### Wake Response Providers (`test_wake_response_providers.py`)

- ✅ JarvisTTS wake response provider
- ✅ Static wake response provider
- ✅ Provider discovery and configuration
- ✅ Error handling for API failures
- ✅ Integration with voice listener
- ✅ Optional provider configuration

## 🛠️ Writing New Tests

### Test Structure

```python
import unittest
from unittest.mock import Mock, patch
from your_module import YourClass

class TestYourClass(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.instance = YourClass()
    
    def test_something(self):
        """Test description"""
        # Arrange
        expected = "expected result"
        
        # Act
        result = self.instance.method()
        
        # Assert
        self.assertEqual(result, expected)
    
    @patch('your_module.external_dependency')
    def test_with_mock(self, mock_dependency):
        """Test with mocked external dependency"""
        mock_dependency.return_value = "mocked result"
        # ... test logic
```

### Best Practices

1. **Use descriptive test names** - Test names should clearly describe what they're testing
2. **Follow AAA pattern** - Arrange, Act, Assert
3. **Mock external dependencies** - Don't rely on external services in tests
4. **Test edge cases** - Include tests for error conditions and boundary values
5. **Use setUp/tearDown** - Clean up resources properly
6. **Test one thing at a time** - Each test should verify one specific behavior

### Mocking Guidelines

```python
# Mock configuration
with patch('module.Config') as mock_config:
    mock_config.get_str.return_value = "test_value"
    
# Mock HTTP requests
with patch('module.httpx.post') as mock_post:
    mock_post.return_value = Mock(content=b"response")
    
# Mock file operations
with patch('builtins.open', mock_open(read_data='{"key": "value"}')):
    # ... test logic

# Mock MQTT messages
mock_msg = Mock()
mock_msg.payload = json.dumps([{"command": "tts", "details": {"message": "test"}}]).encode()

# Mock wake response providers
with patch('core.helpers.get_wake_response_provider') as mock_get_provider:
    mock_provider = Mock()
    mock_provider.fetch_next_wake_response.return_value = "Dynamic greeting!"
    mock_get_provider.return_value = mock_provider
```

## 🔧 Test Configuration

### Environment Variables

Tests can use environment variables for configuration:

```bash
export TEST_MODE=true
export MOCK_EXTERNAL_SERVICES=true
```

### Test Data

Test data should be:
- **Minimal** - Only include what's necessary for the test
- **Realistic** - Use realistic but safe test values
- **Isolated** - Each test should have its own data
- **Clean** - Clean up test data in tearDown

## 🐛 Debugging Tests

### Verbose Output

```bash
python3 run_tests.py --verbose
```

### Single Test Debugging

```bash
# Run single test with more detail
python3 -m unittest tests.test_config_service.TestConfigService.test_get_str -v

# Run with print statements
python3 -c "
import sys
sys.path.insert(0, '.')
from tests.test_config_service import TestConfigService
test = TestConfigService()
test.setUp()
test.test_get_str()
"
```

### Common Issues

1. **Import errors** - Make sure you're running from the project root
2. **Mock not working** - Check the import path in your patch decorator
3. **File not found** - Ensure test files are using temporary files or proper mocking
4. **Dependencies missing** - Install required packages in the virtual environment

## 📊 Test Metrics

### Current Coverage

- **79 tests** across 7 test files
- **97% pass rate** (77/79 tests passing)
- **Comprehensive error handling** coverage
- **Integration testing** for complete workflows
- **MQTT testing** for external communication
- **Wake response testing** for dynamic greetings

### Performance

- **Fast execution** - Tests run in under 5 seconds
- **Minimal dependencies** - Only uses standard library and project dependencies
- **No external calls** - All external services are mocked

### Test Categories Breakdown

- **Unit Tests**: 30 tests
- **Integration Tests**: 18 tests  
- **MQTT Tests**: 16 tests
- **Provider Tests**: 8 tests
- **Wake Response Tests**: 7 tests

## 🎯 Continuous Integration

These tests are designed to be run in CI/CD pipelines:

```yaml
# Example GitHub Actions workflow
- name: Run Tests
  run: |
    cd jarvis-node-setup
    source venv/bin/activate
    python3 run_tests.py
```

## 🚨 Troubleshooting

### Tests Failing on Pi Zero

1. **Check dependencies** - Ensure all packages are installed in the venv
2. **Check paths** - Verify file paths are correct for the Pi environment
3. **Check permissions** - Ensure the pi user has access to test files
4. **Check Python version** - Ensure you're using Python 3.11+

### Common Error Messages

- `ModuleNotFoundError` - Install missing dependencies
- `FileNotFoundError` - Check file paths and mocking
- `AssertionError` - Review test expectations vs actual results
- `ImportError` - Check import paths and module structure

## 📝 Adding New Tests

When adding new functionality:

1. **Create test file** - `tests/test_new_feature.py`
2. **Write unit tests** - Test individual components
3. **Write integration tests** - Test complete workflows
4. **Update this README** - Document new test coverage
5. **Run all tests** - Ensure nothing breaks

## 🧪 MQTT Testing

The MQTT tests cover:

- **Message Format Validation** - Ensures proper JSON structure
- **Command Processing** - Tests TTS command execution
- **Error Handling** - Graceful handling of malformed messages
- **Real-world Scenarios** - Home Assistant, alerts, weather updates
- **Provider Integration** - Works with both JarvisTTS and EspeakTTS

### MQTT Test Examples

```bash
# Test MQTT TTS functionality
python3 run_tests.py --test test_mqtt_tts.py

# Test MQTT integration scenarios
python3 run_tests.py --test test_mqtt_integration.py
```

## 🎤 Wake Response Testing

The wake response tests cover:

- **Provider Discovery** - Automatic provider loading based on config
- **Dynamic Generation** - JarvisTTS API integration for custom greetings
- **Static Behavior** - Fallback to static responses when no provider configured
- **Error Handling** - Graceful handling of API failures
- **Integration** - Works seamlessly with voice listener

### Wake Response Test Examples

```bash
# Test wake response providers
python3 run_tests.py --test test_wake_response_providers.py
```

### Configuration Examples

```json
{
  "wake_response_provider": "jarvis-tts-api",
  "jarvis_tts_api_url": "http://localhost:7707"
}
```

Or for static behavior:
```json
{
  "wake_response_provider": "static"
}
```

Or omit the setting entirely for static behavior.

This testing framework will help you develop faster and with more confidence, catching issues before they reach production! 