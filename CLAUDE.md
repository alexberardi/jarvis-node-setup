# jarvis-node-setup

Client software for Pi Zero voice nodes. Captures audio, detects wake word, sends commands to command-center.

## Quick Reference

```bash
# Run on Pi Zero
python scripts/main.py

# Test
pytest
```

## Architecture

```
jarvis-node-setup/
├── scripts/
│   └── main.py           # Entry point
├── core/
│   ├── ijarvis_command.py      # Command interface (extend this)
│   ├── ijarvis_parameter.py    # Parameter definition
│   ├── command_response.py     # Response structure
│   └── platform_abstraction.py # Hardware abstraction
├── services/
│   ├── network_discovery_service.py  # Network scanning (large file)
│   ├── secret_service.py       # Secret management
│   └── mqtt_tts_listener.py    # MQTT TTS listener
├── commands/                   # Built-in commands (20+)
│   ├── weather_command.py
│   ├── calculator_command.py
│   ├── jokes_command.py
│   └── ...
└── utils/
    └── config_service.py       # Configuration
```

## Extending Commands

Implement `IJarvisCommand`:

```python
from core.ijarvis_command import IJarvisCommand
from core.command_response import CommandResponse

class MyCommand(IJarvisCommand):
    @property
    def name(self) -> str:
        return "my_command"

    @property
    def description(self) -> str:
        return "Does something useful"

    def execute(self, params: dict) -> CommandResponse:
        return CommandResponse(
            success=True,
            message="Done!",
            data={"result": "value"}
        )
```

## Threading Model

- **Main thread**: Voice listener (MQTT voice capture)
- **Background thread**: MQTT listener (TTS commands)

## Wake Word Detection

Uses Porcupine for local wake word detection. Configured in settings.

## Dependencies

- PyAudio, SoundDevice (audio capture)
- paho-mqtt (MQTT integration)
- pvporcupine (wake word)
- httpx (REST client to command-center)
- SQLAlchemy, pysqlcipher3 (local encrypted DB)

## Key Features

- **Plugin architecture**: Add commands via IJarvisCommand
- **Wake word**: Local detection with Porcupine
- **Music Assistant**: Integration for music control
- **Network discovery**: Find other jarvis services
- **Encrypted storage**: PySQLCipher for local secrets

## E2E Testing

### Command Parsing Tests

Tests intent classification and parameter extraction (front half):

```bash
# Run all tests
python test_command_parsing.py

# List all tests
python test_command_parsing.py -l

# Run specific tests by index
python test_command_parsing.py -t 5 7 11

# Run tests for specific commands
python test_command_parsing.py -c calculate get_weather
```

### Multi-Turn Conversation Tests

Tests tool execution, validation flow, and context preservation (back half):

```bash
# Fast mode (text-based, no audio)
python test_multi_turn_conversation.py

# Full mode (TTS → Whisper pipeline)
python test_multi_turn_conversation.py --full

# List all tests
python test_multi_turn_conversation.py -l

# Run specific category
python test_multi_turn_conversation.py -c validation

# Run specific tests with audio artifacts saved
python test_multi_turn_conversation.py --full -t 0 1 2 --save-audio ./audio_artifacts/
```

**Required services for tests:**
- `jarvis-command-center` (port 8002)
- `jarvis-llm-proxy-api` (port 8000)
- For full mode: `jarvis-tts` (port 8009) + `jarvis-whisper-api` (port 8012)

**Service startup:**
```bash
# Command center
cd jarvis-command-center && ./run-docker-dev.sh

# LLM proxy
cd jarvis-llm-proxy-api && ./run.sh

# TTS (for --full mode)
cd jarvis-tts && ./run-docker-dev.sh

# Whisper (for --full mode)
cd jarvis-whisper-api && ./run-dev.sh
```

**Test categories:**
- `tool_execution` - Single-turn tool execution (happy path)
- `validation` - Validation/clarification flow
- `result_incorporation` - Tool results in final response
- `context` - Context preservation across turns
- `error_handling` - Graceful error handling
- `complex` - Complex queries (knowledge, conversions)

## Notes

- This is client software, not a server
- Runs on Pi Zero with mic + speaker
- Communicates with command-center via HTTP
- Receives TTS commands via MQTT
