# Jarvis Node Setup

Client software for Jarvis voice nodes. Runs on Raspberry Pi Zero (or any Linux device) with microphone and speaker, captures audio, detects wake words locally, and sends commands to the Jarvis command center.

## Features

- **Local wake word detection** using [Porcupine](https://picovoice.ai/platform/porcupine/)
- **Plugin architecture** - extend functionality by implementing `IJarvisCommand`
- **20+ built-in commands** - weather, calculator, timers, reminders, music control, and more
- **Encrypted local storage** - secrets stored securely with PySQLCipher
- **Music Assistant integration** - control your media with voice
- **Network discovery** - automatically find Jarvis services on your network

## Quick Start

### Prerequisites

- Python 3.9+
- Pi Zero or compatible Linux device
- Microphone and speaker
- Running [jarvis-command-center](../jarvis-command-center)

### Installation

```bash
# Clone the repo
git clone https://github.com/yourusername/jarvis-node-setup.git
cd jarvis-node-setup

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings
```

### Running

```bash
# Activate venv
source venv/bin/activate

# Run the node
python scripts/main.py
```

## Architecture

```
jarvis-node-setup/
├── scripts/
│   └── main.py               # Entry point
├── core/
│   ├── ijarvis_command.py    # Command interface (extend this)
│   ├── ijarvis_parameter.py  # Parameter definition
│   ├── command_response.py   # Response structure
│   └── platform_abstraction.py
├── services/
│   ├── network_discovery_service.py
│   ├── secret_service.py
│   └── mqtt_tts_listener.py
├── commands/                 # Built-in commands
│   ├── weather_command.py
│   ├── calculator_command.py
│   └── ...
├── clients/
│   └── jarvis_command_center_client.py
└── utils/
    └── config_service.py
```

## Creating Custom Commands

Implement the `IJarvisCommand` interface:

```python
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter
from core.command_response import CommandResponse

class GreetingCommand(IJarvisCommand):
    @property
    def name(self) -> str:
        return "greet"

    @property
    def description(self) -> str:
        return "Greets a person by name"

    @property
    def parameters(self) -> list[IJarvisParameter]:
        return [
            IJarvisParameter(
                name="name",
                description="The person's name",
                param_type=str,
                required=True
            )
        ]

    def execute(self, params: dict) -> CommandResponse:
        name = params.get("name", "friend")
        return CommandResponse(
            success=True,
            message=f"Hello, {name}! Nice to meet you."
        )
```

Place your command file in the `commands/` directory - it will be automatically discovered.

## Built-in Commands

| Command | Description |
|---------|-------------|
| `calculate` | Mathematical calculations |
| `get_weather` | Current weather and forecasts |
| `set_timer` | Set countdown timers |
| `set_reminder` | Schedule reminders |
| `tell_joke` | Random jokes |
| `get_sports_scores` | Sports scores and schedules |
| `control_lights` | Home Assistant light control |
| `play_music` | Music Assistant integration |
| `search_recipe` | Recipe search |
| ... | And many more! |

## Threading Model

- **Main thread**: Voice capture and wake word detection
- **Background thread**: MQTT listener for TTS commands

## Configuration

Key environment variables (see `.env.example`):

| Variable | Description |
|----------|-------------|
| `COMMAND_CENTER_URL` | URL of jarvis-command-center |
| `NODE_API_KEY` | API key for authentication |
| `PORCUPINE_ACCESS_KEY` | Picovoice access key |
| `WAKE_WORD` | Wake word to listen for |
| `MQTT_BROKER` | MQTT broker for TTS |

## Testing

### Unit Tests

```bash
pytest
```

### E2E Command Parsing Tests

Tests intent classification and parameter extraction:

```bash
# Run all tests
python test_command_parsing.py

# List available tests
python test_command_parsing.py -l

# Run specific tests
python test_command_parsing.py -t 5 7 11
python test_command_parsing.py -c calculate get_weather
```

### Multi-Turn Conversation Tests

Tests tool execution, validation flow, and context preservation:

```bash
# Fast mode (text-based)
python test_multi_turn_conversation.py

# Full mode (TTS pipeline)
python test_multi_turn_conversation.py --full

# Run specific category
python test_multi_turn_conversation.py -c validation
```

**Required services for E2E tests:**
- `jarvis-command-center` (port 8002)
- `jarvis-llm-proxy-api` (port 8000)
- For full mode: `jarvis-tts` (port 8009) + `jarvis-whisper-api` (port 8012)

## Dependencies

- **Audio**: PyAudio, SoundDevice
- **Wake word**: pvporcupine
- **MQTT**: paho-mqtt
- **HTTP**: httpx
- **Database**: SQLAlchemy, pysqlcipher3

## Related Services

- [jarvis-command-center](../jarvis-command-center) - Central command processing
- [jarvis-tts](../jarvis-tts) - Text-to-speech
- [jarvis-whisper-api](../jarvis-whisper-api) - Speech-to-text
- [jarvis-llm-proxy-api](../jarvis-llm-proxy-api) - LLM routing

## License

MIT
