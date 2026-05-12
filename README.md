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
- Pi Zero 2 W (or compatible Linux device)
- **Seeed ReSpeaker 2-mics Pi HAT v2** for audio (mic + speaker + RGB LEDs + button) — see [Hardware](#hardware) below
- Running [jarvis-command-center](../jarvis-command-center)

## Hardware

Production nodes use the **Seeed ReSpeaker 2-mics Pi HAT v2** stacked on a Raspberry Pi Zero 2 W. The HAT provides:

| Component | Wiring | Driver |
|-----------|--------|--------|
| TLV320AIC3104 audio codec | I2C bus 1 @ 0x18 + I2S | Seeed's `respeaker-2mic-v2_0-overlay` (DTS vendored at `setup/respeaker-2mic-v2_0-overlay.dts`, compiled at install time) |
| Stereo electret mics | analog → AIC3104 ADC (LINE1L/LINE1R) | exposed as ALSA card `seeed2micvoicec` |
| Speaker output (JST 2.0) | analog ← AIC3104 HP path | same ALSA card, playback path |
| 3× APA102 RGB LEDs | SPI (MOSI=GPIO10, SCLK=GPIO11) | `apa102-pi` via `services/respeaker_led_service.py` |
| User button | GPIO17, active-low | `gpiozero` via `services/button_service.py` |
| Grove I2C ports (×2) | I2C bus 1 | available for sensors |

> **HAT version matters.** The original v1.0 HAT used a WM8960 codec; Seeed swapped to TLV320AIC3104 for v2.0 (no shipping kernel overlay covers v2.0 out-of-the-box, so we vendor the DTS and `dtc`-compile it during install). Visual cue: v2.0 has a black silkscreen "v2.0" near the user button.

`install.sh` apt-installs `device-tree-compiler`, compiles `setup/respeaker-2mic-v2_0-overlay.dts` into `/boot/firmware/overlays/`, configures `/boot/firmware/config.txt` with `dtoverlay=respeaker-2mic-v2_0-overlay`, `dtparam=spi=on`, `dtparam=i2c_arm=on`, and rewrites `/etc/asound.conf` to expose the HAT to the app under the existing alias names (`dsnoopmic` for capture, `output` for playback) so app-level config doesn't change when hardware does.

**Button behavior:**

- **Short press (<3 s)** publishes MQTT topic `jarvis/nodes/<node_id>/button/notifications_request`. The command-center handles this by speaking any queued notifications through the node's TTS path.
- **Long hold (≥3 s)** publishes `jarvis/nodes/<node_id>/button/shutdown` and runs `sudo systemctl poweroff` for a clean shutdown. The RGB LEDs flash red at the 1 s mark as a "you're holding for shutdown" warning.
- **Power-on caveat:** the Pi Zero 2 W has no software wake-on-GPIO from a halted state. After a button-triggered shutdown, the node must be power-cycled (unplug/replug) to come back up. There's no software workaround for this — the Pi RUN pin can be soldered for a hardware fix, but we don't ship that.

**LED state map** (`set_pattern` + `set_transient_pattern` in `LEDService` / `RespeakerLEDService`):

| Pattern | When | Visual (RGB / ACT-LED fallback) |
|---------|------|---------------------------------|
| `normal` | Idle | dim white / kernel default |
| `alert` | Alert queue ≥1 item | red 1 Hz blink / red 1 Hz blink |
| `listening` | After wake word, capturing audio | blue chase / kernel default |
| `thinking` | STT + LLM processing | purple pulse / kernel default |
| `speaking` | TTS playing | cyan steady / kernel default |
| `shutdown_warning` | Power button held ≥1 s | red 5 Hz blink / red 5 Hz blink |

`get_led_service()` auto-detects the APA102 chain on `/dev/spidev0.0`; when absent (no HAT, or `apa102-pi` not installed), it falls back to driving the Pi's built-in ACT LED, which only renders `normal`, `alert`, and `shutdown_warning` distinctly.

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
from jarvis_command_sdk import IJarvisCommand
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

Tests intent classification and parameter extraction across 82 voice commands covering weather, calendar, sports, timers, calculator, Home Assistant, and more.

```bash
# Run all tests
python test_command_parsing.py

# List available tests
python test_command_parsing.py -l

# Run specific tests
python test_command_parsing.py -t 5 7 11
python test_command_parsing.py -c calculate get_weather
```

#### Benchmark Results

All benchmarks run on the 82-test E2E suite with `Llama32_3B_Compressed` prompt provider (text-based `<function=name>{args}</function>` tool calling). Server-side HA entity resolution enabled.

| Model | Quant | Size | Score | Avg Latency | Notes |
|-------|-------|------|-------|-------------|-------|
| Llama 3.2 3B Instruct | f16 | 6.0 GB | **90.2%** (74/82) | 1.19s | Recommended for 3B tier |
| Llama 3.2 3B Instruct | Q4_K_M | 1.9 GB | **84.2%** (69/82) | 0.82s | For memory-constrained hardware (Pi 5 4GB, AI HAT+) |

**8B model benchmarks** (using model-specific prompt providers):

| Model | Quant | Size | Score | Avg Latency | Notes |
|-------|-------|------|-------|-------------|-------|
| Qwen 2.5 7B Instruct | Q4_K_M | 4.4 GB | **95.1%** (78/82) | 1.10s | Best overall (compressed provider) |
| Llama 3.1 8B Instruct | Q6_K | 6.1 GB | **93.1%** (67/72) | 1.30s | Strong general-purpose |
| Gemma 2 9B Instruct | Q4_K_M | 5.4 GB | **93.1%** (67/72) | 2.50s | Accurate but slower |
| Hermes 3 8B (Llama 3.1) | Q4_K_M | 4.6 GB | **91.5%** (75/82) | 1.38s | Good with custom prompts |

*8B benchmarks from 72-test or 82-test suites depending on when last run. 3B benchmarks include HA tests (server-side entity resolution).*

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
- `jarvis-command-center` (port 7703)
- `jarvis-llm-proxy-api` (port 7704)
- For full mode: `jarvis-tts` (port 7707) + `jarvis-whisper-api` (port 7706)

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
