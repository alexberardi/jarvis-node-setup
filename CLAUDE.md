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

## Notes

- This is client software, not a server
- Runs on Pi Zero with mic + speaker
- Communicates with command-center via HTTP
- Receives TTS commands via MQTT
