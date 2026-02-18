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

**Python Libraries:**
- PyAudio, SoundDevice (audio capture)
- paho-mqtt (MQTT integration)
- pvporcupine (wake word)
- httpx (REST client to command-center)
- SQLAlchemy, pysqlcipher3 (local encrypted DB)

**Service Dependencies:**
- ✅ **Required**: `jarvis-command-center` (7703) - Voice command processing
- ⚠️ **Optional**: `jarvis-tts` (7707) - Text-to-speech for responses
- ⚠️ **Optional**: `jarvis-config-service` (7700) - Service discovery

**Used By:**
- End users (voice interaction via Pi Zero nodes)

**Impact if Down:**
- ⚠️ That specific node cannot capture voice input
- ✅ Other nodes continue to work
- ✅ All backend services continue to work

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
- `jarvis-command-center` (port 7703)
- `jarvis-llm-proxy-api` (port 7704)
- For full mode: `jarvis-tts` (port 7707) + `jarvis-whisper-api` (port 7706)

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

## Provisioning

For headless Pi Zero nodes, provisioning allows the mobile app to bootstrap WiFi and register with command center.

### Automatic Provisioning

When `main.py` starts and the node is not provisioned, it **automatically enters provisioning mode**:

1. Starts AP mode (creates `jarvis-XXXX` WiFi network)
2. Runs provisioning API server on port 8080
3. Waits for mobile app to provision
4. After successful provisioning, auto-restarts in normal mode

This means a fresh node just needs to run `main.py` (or the systemd service) - no manual switching between modes.

### Manual Provisioning Server (Development)

```bash
# Start in simulation mode (for development/testing on Ubuntu/macOS)
JARVIS_SIMULATE_PROVISIONING=true python scripts/run_provisioning.py

# Start with real WiFi (on Pi)
sudo python scripts/run_provisioning.py
```

Server runs on port 8080 (configurable via `JARVIS_PROVISIONING_PORT`).

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/info` | GET | Node info (id, firmware, mac, capabilities, state) |
| `/api/v1/scan-networks` | GET | Available WiFi networks |
| `/api/v1/provision/k2` | POST | Send K2 encryption key (for settings sync) |
| `/api/v1/provision` | POST | Send WiFi creds + room + command center URL |
| `/api/v1/status` | GET | Provisioning progress |

### Provisioning States

- `AP_MODE` - Waiting for mobile app connection
- `CONNECTING` - Attempting to connect to home WiFi
- `REGISTERING` - Registering with command center
- `PROVISIONED` - Successfully provisioned
- `ERROR` - Error occurred

### Files

```
provisioning/
├── __init__.py
├── api.py              # FastAPI application
├── models.py           # Pydantic models
├── registration.py     # Command center registration
├── startup.py          # Provisioning detection
├── state_machine.py    # State management
├── wifi_credentials.py # Encrypted credential storage
└── wifi_manager.py     # WiFi operations interface
```

### Provisioned Files

After provisioning, these files are created in `~/.jarvis/`:

| File | Description |
|------|-------------|
| `secrets.key` | K1 master key (Fernet, created on first run) |
| `k2.enc` | K2 settings key (encrypted with K1) |
| `k2_metadata.json` | K2 key ID and creation timestamp |
| `wifi_credentials.enc` | WiFi credentials (encrypted with K1) |
| `.provisioned` | Marker file indicating provisioning complete |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JARVIS_SIMULATE_PROVISIONING` | false | Use simulated WiFi manager |
| `JARVIS_PROVISIONING_PORT` | 8080 | Provisioning API port |
| `JARVIS_SKIP_PROVISIONING_CHECK` | false | Skip provisioning check on main.py startup |
| `JARVIS_WIFI_BACKEND` | networkmanager | WiFi backend (`networkmanager` or `hostapd`) |

## Notes

- This is client software, not a server (except provisioning mode)
- Runs on Pi Zero with mic + speaker
- Communicates with command-center via HTTP
- Receives TTS commands via MQTT
