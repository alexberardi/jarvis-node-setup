# jarvis-node-setup

Client software for Pi Zero voice nodes. Captures audio, detects wake word, sends commands to command-center.

## Installers

Two install paths exist — use the right one for the situation:

- **`install.sh`** — production installer. Curl-piped from a GitHub release tag, runs as root on the Pi, installs to `/opt/jarvis-node`, writes `/etc/modprobe.d/alsa-base.conf` and `/etc/asound.conf`, creates the systemd unit. This is what real Pi Zero nodes run. Any audio / system / service config change for production must land here.
- **`setup/pi.sh`, `setup/macos.sh`, `setup/ubuntu.sh`** — dev-machine setup scripts (clone repo, install deps into a local `.venv`). Not used by Pi nodes anymore. Treat `setup/pi.sh` as legacy reference; if you change audio config, mirror it into `install.sh` (the two have drifted before — see commit `08d2e1f`).

## Quick Reference

```bash
# Run on Pi Zero
python scripts/main.py

# Test
pytest
```

## Dev Setup

### 0. Install dependencies

```bash
cd jarvis-node-setup
python3 -m venv .venv
.venv/bin/pip install -e ../jarvis-command-sdk   # Core SDK (monorepo sibling)
.venv/bin/pip install -e .                       # Node dependencies
```

### 1. Install commands (seed secrets DB)

Discovers all command classes, runs DB migrations, and seeds the secrets table
with empty-value rows for each command's `required_secrets`. Existing values are
never overwritten.

```bash
cd jarvis-node-setup

# List all commands and their secrets
python scripts/install_command.py --list

# Install all commands (run migrations + seed secrets)
python scripts/install_command.py --all

# Install a single command
python scripts/install_command.py get_weather
```

### 2. Generate dev K2 (for mobile settings sync)

K2 is a shared AES-256 key between the node and mobile app. In production it's
exchanged during WiFi provisioning. For dev, generate it manually:

```bash
python utils/generate_dev_k2.py          # generates K2, saves to ~/.jarvis/k2.enc
python utils/generate_dev_k2.py --force  # overwrite existing K2
```

Outputs a base64url string to paste into the mobile app.

### 3. Import K2 into mobile app (iOS Simulator)

The QR scanner doesn't work in the simulator, so there's a dev-only paste input:

1. Open the iOS Simulator (`npm run ios` in jarvis-node-mobile)
2. Go to **Nodes** tab
3. Tap **Import Key** (top-right)
4. Scroll down to the **DEV: Paste key data** input at the bottom
5. Paste the base64url string from step 2
6. Tap **Import**

Both sides now share K2 and settings sync will work.

### 4. Test the settings flow

1. Start required services (command-center, MQTT broker)
2. Tap a node in the Nodes tab to open its settings
3. The mobile app requests a snapshot via CC, which notifies the node via MQTT
4. The node builds a snapshot, encrypts with K2, uploads to CC
5. The mobile polls, decrypts, and displays command settings

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
│   ├── secret_service.py       # Secret management
│   ├── mqtt_tts_listener.py    # MQTT TTS listener
│   ├── command_store_service.py # Pantry install/remove/list
│   ├── reminder_service.py     # Reminder CRUD, recurrence, snooze
│   └── storage_backend.py      # JarvisStorage → SessionLocal bridge
├── routines/
│   └── custom_routines/        # Pantry-installed routine JSON files
├── ha_shared/                  # Home Assistant shared code
│   ├── home_assistant_service.py  # HA WebSocket client + actions
│   └── entity_resolver.py     # Fuzzy entity ID matching
├── commands/                   # Built-in commands (20+)
│   ├── weather_command.py
│   ├── calculator_command.py
│   ├── control_device/command.py   # HA device control (convention layout)
│   ├── get_device_status/command.py
│   ├── reminder_command.py         # Set/list/delete/snooze reminders
│   ├── routine_command.py          # Multi-step voice routines
│   └── ...
├── agents/                     # Background agents
│   ├── home_assistant/agent.py     # HA state caching (convention layout)
│   └── reminder_agent.py           # Background agent for due reminders
├── device_managers/            # Device listing backends
│   └── home_assistant/manager.py   # HA device listing (convention layout)
└── utils/
    └── config_service.py       # Configuration
```

## Shared Code Pattern (`*_shared/` directories)

When multiple components (commands, agents, device managers) share code, put it
in a `<feature>_shared/` package at the project root — **not** in `services/` or
`utils/`, which are reserved for node framework code.

**Example: Home Assistant**

```
ha_shared/
├── __init__.py
├── home_assistant_service.py   # HA WebSocket client, actions, state queries
└── entity_resolver.py          # Fuzzy entity ID matching
```

Components import from the shared package:
```python
from ha_shared.home_assistant_service import HomeAssistantService
from ha_shared.entity_resolver import resolve_entity_id
```

**Why not `services/` or `utils/`?**

Community packages installed from the Pantry scatter components to type-specific
directories and install shared code to `~/.jarvis/packages/<name>/lib/`. If a
package ships a `services/` directory, it shadows the node's built-in `services/`
package. The Pantry static analysis pipeline flags this with a warning.

**Convention:**
- `ha_shared/` — Home Assistant shared code
- `<package>_shared/` — any integration's shared code
- Node framework code stays in `services/`, `utils/`, `core/`

## Pantry CLI (Command Store)

Install, remove, and manage packages from the Pantry:

```bash
# Install from GitHub
python scripts/command_store.py install --url https://github.com/user/jarvis-my-command

# Install from local directory (dev/testing)
python scripts/command_store.py install --local /path/to/package

# Install from store catalog
python scripts/command_store.py install my_command

# Remove
python scripts/command_store.py remove package_name

# List installed
python scripts/command_store.py list
```

### Bundle Install Layout

Bundles scatter components to type-specific directories:

| Component type | Install dir |
|----------------|-------------|
| `command` | `commands/custom_commands/{name}/` |
| `agent` | `agents/custom_agents/{name}/` |
| `device_protocol` | `device_families/custom_families/{name}/` |
| `device_manager` | `device_managers/custom_managers/{name}/` |
| `routine` | `routines/custom_routines/{name}/` |

Shared code → `~/.jarvis/packages/{name}/lib/`
Package metadata → `~/.jarvis/packages/{name}.json`

### Convention Directory Structure (for repos)

The Pantry infers component types from directory layout when `components` is not
declared in the manifest:

```
commands/<name>/command.py          → command
agents/<name>/agent.py              → agent
device_families/<name>/protocol.py  → device_protocol
device_managers/<name>/manager.py   → device_manager
command.py (at root)                → single command
routines/<name>/routine.json        → routine
routine.json (at root)              → single routine
```

### Reference Bundle

[jarvis-home-assistant-integration](https://github.com/alexberardi/jarvis-home-assistant-integration) —
4 components (2 commands + 1 agent + 1 device manager) with `ha_shared/` for
shared code. Use this as a template for new bundles.

### Validate a Package

Test that a package installs correctly without actually installing:

```bash
python scripts/command_store.py validate /path/to/package
```

Checks manifest, component paths, and import-tests commands/agents/protocols. Validates routine JSON structure for routine components. Skips platform checks.

## Extending Commands

Implement `IJarvisCommand`:

```python
from jarvis_command_sdk import IJarvisCommand
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

## Node Authentication (Dev Setup)

Nodes authenticate to the command center via `X-API-Key: {node_id}:{api_key}`. For local development and E2E tests, you must register a node.

### Register a Dev Node

The `authorize_node.py` script handles registration via the command center's admin API. It needs:
1. The CC `ADMIN_API_KEY` (from `jarvis-command-center/.env`)
2. A household ID (use `--list` first or `--create-household`)

```bash
# Step 1: Get the admin key from CC's .env
grep ADMIN_API_KEY ../jarvis-command-center/.env
# → ADMIN_API_KEY=a908...

# Step 2: List existing households (to get household_id)
python utils/authorize_node.py --cc-key <admin_key> --list

# Step 3: Register node and auto-update config-mac.json
python utils/authorize_node.py \
  --cc-key <admin_key> \
  --household-id <household-uuid> \
  --room office \
  --name dev-mac \
  --update-config config-mac.json
```

This creates a provisioning token, registers the node with jarvis-auth, and writes the new `node_id` and `api_key` into config-mac.json.

### Verify Auth Works

```bash
curl -s http://localhost:7703/api/v0/health \
  -H "X-API-Key: $(python -c 'import json; c=json.load(open("config-mac.json")); print(f"{c[\"node_id\"]}:{c[\"api_key\"]}")')"
```

### Common Auth Issues

- **401 Unauthorized on E2E tests**: Node credentials in `config-mac.json` are not registered. Re-run `authorize_node.py` with `--update-config`.
- **"Invalid Admin API Key"**: The default `admin_key` does not work for write operations. Get the real key from `jarvis-command-center/.env`.
- **Node already exists**: Use `--delete` first, then re-register.

## E2E Testing

### Prerequisites

1. **Register a dev node** (see [Node Authentication](#node-authentication-dev-setup) above)
2. **Start required services:**

```bash
# Command center
cd jarvis-command-center && ./run-docker-dev.sh

# LLM proxy
cd jarvis-llm-proxy-api && ./run.sh

# TTS (for --full mode only)
cd jarvis-tts && ./run-docker-dev.sh

# Whisper (for --full mode only)
cd jarvis-whisper-api && ./run-dev.sh
```

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

**Required services:**
- `jarvis-command-center` (port 7703)
- `jarvis-llm-proxy-api` (port 7704)
- For full mode: `jarvis-tts` (port 7707) + `jarvis-whisper-api` (port 7706)

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
