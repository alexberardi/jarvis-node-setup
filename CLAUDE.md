# jarvis-node-setup

Client software for **Pi Zero voice nodes**. Captures audio, detects wake word, ships voice to command-center, plays TTS audio back. Also hosts the **plugin runtime** for community packages (commands, agents, device protocols, device managers, routines) installed via Pantry.

> **Identity rule:** the node is the *edge* — it owns mic/speaker hardware, wake-word detection, and the plugin runtime. The brain lives in command-center. If you find yourself building "smart" routing logic on the node, push it to CC.

---

## Topology

```
                       Wake word (openWakeWord, local)
                              │
                              ▼
   ┌───────────────────────────────────────────────────┐
   │  jarvis-node-setup (running on Pi Zero or Mac)     │
   │                                                     │
   │  ┌──────────────────┐    ┌─────────────────────┐  │
   │  │  Voice loop      │    │  Plugin runtime     │  │
   │  │  - mic capture   │    │  - commands/        │  │
   │  │  - wake detect   │    │  - agents/          │  │
   │  │  - audio out     │    │  - device_families/ │  │
   │  └────────┬─────────┘    │  - routines/        │  │
   │           │               └─────────────────────┘  │
   │           │ HTTP (X-API-Key: node_id:api_key)      │
   │           ▼                                         │
   └───────────┬───────────────────────────────────────┘
               │
               ▼
   ┌─────────────────────────────────────────────────────┐
   │  jarvis-command-center :7703 (the brain)            │
   │  /conversation/start → /voice/command/stream        │
   │  → audio bytes streamed back to node speaker        │
   └─────────────────────────────────────────────────────┘
               ▲                                ▲
   MQTT subscribe (server → node)         HTTP responses
   - TTS messages                              ▲
   - settings push                             │
   - package install                           │
   - bluetooth ops                             │
   - node updates                              │
   - reminders                                 │
                                               │
   ┌───────────────────────────────────────────┴─────────┐
   │ Optional / on demand:                                │
   │ - jarvis-whisper-api (STT via CC media proxy)       │
   │ - jarvis-tts (audio synthesis via CC streaming)     │
   │ - jarvis-pantry (package install via MQTT push)     │
   └──────────────────────────────────────────────────────┘
```

---

## Voice loop lifecycle (the hot path)

```
1. Node boots → main.py
   ├─ if not provisioned: enter provisioning mode (AP WiFi + port 8080) — see "Provisioning"
   └─ if provisioned: start voice loop + MQTT listener

2. Wake word detection (main thread):
   ├─ openWakeWord listens to mic continuously (`scripts/voice_listener.py`,
   │  model defaults to `hey_jarvis` via the `wake_word_model` setting; the
   │  Pi USB mic captures 48kHz audio and we resample to 16kHz before scoring)
   └─ on wake → call `/voice/acknowledge` for instant ack ("Sure", "On it")
       └─ also start recording the user's utterance

3. STT path:
   ├─ record audio chunk (silence-bounded)
   ├─ POST to CC `/api/v0/media/whisper/transcribe` (CC proxies to jarvis-whisper-api)
   └─ get text back

4. Voice command path:
   ├─ POST `/conversation/start` once per session
   │   - sends client_tools[] (installed commands), available_commands[],
   │     node_context { speaker_user_id, agents, timezone }
   ├─ POST `/voice/command/stream` (the streaming hot path)
   │   - 200 audio/raw PCM → write to speaker
   │   - 202 JSON with tool_calls → execute locally, post back to
   │     `/voice/command/continue/stream`
   └─ loop until done

5. MQTT background thread:
   ├─ subscribes to per-node topics
   └─ handles inbound: TTS-by-text, settings updates, package install,
       bluetooth commands, node updates, reminders due
```

Multi-step workflows (tool calls):
- Node receives 202 JSON listing `tool_calls`
- Looks up each tool in its local command registry (`commands/`)
- Runs the command(s)
- POSTs results to `/voice/command/continue/stream`
- CC issues the final assistant message; node plays it as audio

---

## Dependency graph

**Upstream (node depends on):**
- **jarvis-command-center** (required, port 7703) — all voice traffic
- **jarvis-auth** indirectly via CC (node validation; node never calls auth directly)
- **MQTT broker** (required, port 1883/1884) — for inbound server→node messages
- **jarvis-tts**, **jarvis-whisper-api** — proxied through CC; node doesn't talk to them directly
- **jarvis-pantry** (optional, for package install)
- **Local Postgres / SQLite via SQLAlchemy + pysqlcipher3** — encrypted local DB for secrets, reminders, packages

**Downstream (depends on node):**
- **The user**, in the room with the node.

**Impact if down (one node):**
- That node loses voice; other nodes and backend services unaffected.

---

## Invariants & gotchas

1. **Two install paths exist** — production (`install.sh` curl-piped from a GitHub release tag, installs to `/opt/jarvis-node`) and dev (`setup/*.sh`, into a local `.venv`). **Audio config drift between them has burned us before** (commit `08d2e1f`). If you change ALSA settings, update `install.sh` first.
2. **`*_shared/` for cross-component code, not `services/`/`utils/`/`core/`.** Built-in node directories shadow community-package shared dirs because everything goes on `sys.path`. Pantry static analysis flags this as a warning. **Always use a package-specific name** (`ha_shared/`, `lifx_shared/`, etc.).
3. **`jarvis_dependencies` in package manifest creates an importable namespace at install time.** A package can extend another via class inheritance (e.g. `nest_pro` extending `nest`). The dependency must be installed *first*; uninstalling a depended-on package is blocked.
4. **The local DB uses SQLCipher.** It's an encrypted SQLite. Reading directly via `sqlite3` won't work without the key. Always go through SQLAlchemy + the storage backend.
5. **K1 vs K2:** K1 is the node's master key (Fernet, generated on first run, never leaves the node). K2 is a shared AES-256 key with the mobile app for settings sync. K2 is generated at provisioning time (or manually for dev via `utils/generate_dev_k2.py`).
6. **Wake-acknowledge and the main voice request run in parallel.** `/voice/acknowledge` is a fast no-LLM keyword match; `/voice/command/stream` is the real work. Don't refactor them into a single call — the user-perceived latency benefit of parallel ack is significant.
7. **`/voice/command/stream` returns 200 (audio) OR 202 (JSON).** The node must branch on content-type — audio for "spoken response", JSON for "tool calls to run". Both are normal outcomes.
8. **MQTT is the only server→node async channel.** No SSE-to-node, no WebSocket push. If you need to deliver a message to the node without it asking, use MQTT.
9. **Node auth is `X-API-Key: node_id:api_key`**, not a JWT and not app-creds. This is the only auth pattern that uses that header format in the stack — match it carefully.
10. **`authorize_node.py` is for dev only.** Production nodes get credentials via the provisioning flow (mobile app → AP WiFi → exchange tokens). Don't use the dev script in production.
11. **Many results files (`*_results.json`, `round*_*.json`) at the repo root are experiment artifacts.** They're not tracked test data. Don't reorganize them without checking with the user — some experiments are still being referenced.
12. **Don't talk to whisper/tts directly.** Always proxy through CC's `/api/v0/media/*`. Direct calls would bypass auth context headers (household_id, member_ids) that voice recognition + speaker resolution depend on.

---

## Failure modes

| Failure | Behavior |
|---|---|
| Command-center unreachable | Voice hangs at first request; node retries on next wake |
| MQTT broker down | Server→node messages are dropped; voice still works |
| Whisper down (via CC proxy) | STT fails → no transcript → voice command fails |
| TTS down | Streaming voice path fails; node falls back to text-only display (if available) |
| Local DB corrupted | Node fails to start; restore from `~/.jarvis/` backup |
| openWakeWord model missing | Wake word detection disabled; node falls back to keyboard listener (when running with a TTY) or starts mute |
| Provisioning not yet done | Node starts in AP mode (jarvis-XXXX WiFi); user pairs via mobile |
| Package install fails mid-way | Partial files left in `commands/custom_commands/`; remove via `command_store.py remove` |

---

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
directories and install shared code to `~/.jarvis/packages/<name>/<name>_lib/`. If a
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

Shared code → `~/.jarvis/packages/{name}/{name}_lib/`
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

## Creating Packages

Packages are standalone repos that extend the node with commands, device protocols,
agents, or device managers. The Pantry installs them by scattering components to
type-specific directories.

### Package Repo Structure

Every package needs a `jarvis_package.yaml` manifest at the root. The directory
layout determines component types:

```
my-package/
├── jarvis_package.yaml          # Required manifest
├── README.md
├── LICENSE
├── .gitignore
├── commands/<name>/command.py   # IJarvisCommand implementation
└── device_families/<name>/      # IJarvisDeviceProtocol implementation
    ├── __init__.py
    ├── protocol.py              # Protocol class
    └── my_client.py             # REST/API client (optional)
```

### Manifest (jarvis_package.yaml)

```yaml
name: "my_package"
display_name: "My Package"
description: "What it does"
version: "1.0.0"
min_jarvis_version: "0.9.0"
license: "MIT"
author:
  github: "username"
categories: ["smart-home", "security"]
platforms: ["darwin", "linux"]
keywords: ["relevant", "search", "terms"]

components:
  - type: device_protocol      # or: command, agent, device_manager, routine
    name: my_protocol
    path: device_families/my_protocol/protocol.py

packages:                       # pip dependencies
  - name: httpx

jarvis_dependencies:             # depend on other Jarvis packages
  - nest                         # must be installed first

secrets:
  - key: MY_API_KEY
    scope: integration          # or: node, user
    value_type: string
    description: "API key for the service"
    sensitive: true
```

### Reference Repos

Use these as templates when creating new packages:

| Type | Repo | Notes |
|------|------|-------|
| Command | `jarvis-cmd-meteo-weather` | Simple REST command with secrets |
| Device protocol (cloud) | `jarvis-device-schlage` | Cloud API with custom auth client |
| Device protocol (cloud+OAuth) | `jarvis-device-simplisafe` | OAuth2+PKCE, token rotation, AlarmControl UI |
| Device protocol (LAN) | `jarvis-device-govee` | Hybrid cloud/LAN discovery |
| Multi-component bundle | `jarvis-home-assistant-integration` | 2 commands + 1 agent + 1 device manager + shared code |

### Extending Existing Packages (jarvis_dependencies)

Packages can inherit from and extend other installed packages. Declare
`jarvis_dependencies` in the manifest to import classes from a dependency:

```yaml
# jarvis_package.yaml
name: "nest_pro"
jarvis_dependencies:
  - nest                  # must be installed first
components:
  - type: device_protocol
    name: nest_pro
    path: device_families/nest_pro/protocol.py
```

```python
# device_families/nest_pro/protocol.py
from nest import NestProtocol     # auto-generated namespace

class NestProProtocol(NestProtocol):
    """Extends Nest with energy monitoring."""

    @property
    def protocol_name(self) -> str:
        return "nest_pro"

    @property
    def supported_domains(self) -> list[str]:
        return [*super().supported_domains, "energy"]
```

**How it works:**
- When a package is installed, an importable namespace is auto-generated at
  `~/.jarvis/packages/<name>/__init__.py` that re-exports component classes
- `~/.jarvis/packages/` is on `sys.path`, so `from nest import NestProtocol` works
- `issubclass()` is transitive — runtime discovery handles inheritance chains
- Uninstalling a dependency is blocked if other packages depend on it

### Writing a Command (IJarvisCommand)

Commands handle voice intents. The LLM parses voice → selects command → extracts
parameters → calls `run()`.

```python
from jarvis_command_sdk import IJarvisCommand, JarvisParameter, JarvisSecret, CommandResponse, RequestInformation, JarvisStorage

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging
    class JarvisLogger:
        def __init__(self, **kw): self._log = logging.getLogger(kw.get("service", __name__))
        def info(self, msg, **kw): self._log.info(msg)
        def error(self, msg, **kw): self._log.error(msg)

logger = JarvisLogger(service="cmd.my_command")
_storage = JarvisStorage("my_command")

class MyCommand(IJarvisCommand):
    @property
    def command_name(self) -> str: return "my_command"

    @property
    def keywords(self) -> list[str]: return ["my command", "do thing"]

    @property
    def description(self) -> str: return "Does something useful"

    @property
    def parameters(self) -> list[JarvisParameter]:
        return [JarvisParameter("query", "string", "What to look up", required=True)]

    @property
    def required_secrets(self) -> list[JarvisSecret]:
        return [JarvisSecret("MY_API_KEY", "API key", "integration", "string", is_sensitive=True, required=True)]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        query = kwargs.get("query", "")
        api_key = _storage.get_secret("MY_API_KEY", scope="integration")
        if not api_key:
            return CommandResponse.error_response(error_details="API key not configured")
        # ... do work ...
        return CommandResponse.success_response(message="Done!", context_data={"result": "value"})
```

### Writing a Device Protocol (IJarvisDeviceProtocol)

Device protocols handle discovery and control for a device manufacturer/API.
They're used by the `control_device` command.

```python
from jarvis_command_sdk import (
    IJarvisDeviceProtocol, DiscoveredDevice, DeviceControlResult,
    IJarvisButton, JarvisSecret, JarvisStorage,
)

class MyProtocol(IJarvisDeviceProtocol):
    protocol_name: str = "my_protocol"
    friendly_name: str = "My Protocol"
    supported_domains: list[str] = ["switch", "light"]  # HA-style domains
    connection_type: str = "cloud"  # or: "lan", "hybrid"

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [JarvisSecret("MY_API_KEY", "API key", "integration", "string", is_sensitive=True, required=True)]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Turn On", "turn_on", "primary", "power"),
            IJarvisButton("Turn Off", "turn_off", "secondary", "power-off"),
        ]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        # Query API/scan network, return found devices
        return [DiscoveredDevice(
            entity_id="living_room_light",
            name="Living Room Light",
            domain="light",
            protocol=self.protocol_name,
            model="Model X",
            manufacturer="My Brand",
            cloud_id="device-123",        # Cloud API identifier
        )]

    async def control(self, device: DiscoveredDevice, action: str, params: dict | None = None) -> DeviceControlResult:
        # Send control command to device
        return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)

    async def get_state(self, device: DiscoveredDevice) -> dict:
        # Query current state
        return {"is_on": True, "brightness": 80}
```

**Key patterns:**
- Use `asyncio.to_thread()` to wrap sync API calls (see Schlage, SimpliSafe)
- Cache authenticated clients at module level to avoid re-auth on every call
- Return structured errors via `DeviceControlResult(success=False, error="...")`, never raise
- Store/rotate tokens via `JarvisStorage`
- Entity IDs must be unique — prefix with domain if names can collide (e.g., `sensor_front_door` vs `lock_front_door`)

### OAuth Device Protocols

For devices requiring OAuth (e.g., SimpliSafe), declare `AuthenticationConfig`:

```python
from jarvis_command_sdk.authentication import AuthenticationConfig

@property
def authentication(self) -> AuthenticationConfig:
    return AuthenticationConfig(
        type="oauth",
        provider="my_provider",
        friendly_name="My Service",
        client_id="...",
        keys=["refresh_token"],
        authorize_url="https://auth.example.com/authorize",
        exchange_url="https://auth.example.com/oauth/token",
        supports_pkce=True,
        native_redirect_uri="com.example.app://callback",
        scopes=["openid", "offline_access", "https://api.example.com/scopes/user:platform"],
    )
```

The mobile app reads this config from the settings snapshot and handles the
OAuth flow natively. Implement `store_auth_values()` to persist tokens.

### Mobile UI for Device Protocols

When the mobile app discovers a device with a specific `domain`, it renders
a domain-specific control component. Mapping in `DeviceControlPanel.tsx`:

| Domain | Control | Actions |
|--------|---------|---------|
| `light` | LightControl | turn_on, turn_off, set_brightness, set_color |
| `switch` | SwitchControl | turn_on, turn_off |
| `lock` | LockControl | lock, unlock |
| `climate` | ClimateControl | set_temperature, set_mode |
| `cover` | CoverControl | open, close, set_position |
| `security_system` | AlarmControl | arm_home, arm_away, disarm |
| `camera` | CameraControl | (read-only) |
| `kettle` | KettleControl | boil, keep_warm, turn_off |
| _(unknown)_ | ActionButtons | Falls back to `supported_actions` buttons |

To add a new domain-specific control: create a component in
`jarvis-node-mobile/src/components/device-controls/`, add it to the
`DOMAIN_TO_CONTROL_TYPE` map and switch statement in `DeviceControlPanel.tsx`.

The control reads device state from `get_state()` on the protocol and sends
actions via the CC → MQTT → node → protocol pipeline.

### Cross-Node Package & Secret Sync

**Installing on multiple nodes:**
- Pantry store packages can be installed on any node via the Store tab
- The install flow supports multi-node selection (NodePickerSheet)
- Node handler (`package_install_handler.py`) is generic — handles all component types

**Syncing secrets between nodes:**
- Node settings → device family card → "Sync to other nodes"
- Picks target nodes → picks which secrets → encrypts with target K2 → pushes
- Both command and device protocol secrets are synced (extracts from `snapshot.commands` and `snapshot.device_families`)
- Target node must have K2 imported (use "Export Encryption Key" on source node settings)

**Installing on a Pi (production):**
```bash
# From dev machine:
scp -r /path/to/package pi@jarvis-dev.local:/tmp/my-package
ssh -t pi@jarvis-dev.local "sudo /opt/jarvis-node/.venv/bin/python /opt/jarvis-node/scripts/command_store.py install --local /tmp/my-package"
ssh -t pi@jarvis-dev.local "sudo systemctl restart jarvis-node"
```

## Threading Model

- **Main thread**: Voice listener (MQTT voice capture)
- **Background thread**: MQTT listener (TTS commands)

## Wake Word Detection

Uses [openWakeWord](https://github.com/dscripka/openWakeWord) for local, no-cloud wake-word detection. The model name is configured via the `wake_word_model` setting (defaults to `hey_jarvis`); models are downloaded on first run via `openwakeword.utils.download_models`. Audio captured from the mic at 48kHz is downsampled to 16kHz (the model's expected input rate) before scoring — see `scripts/voice_listener.py` for the loop, and `core/barge_in.py` for the parallel barge-in detector that listens during TTS playback.

## Dependencies

**Python Libraries:**
- PyAudio, SoundDevice (audio capture)
- paho-mqtt (MQTT integration)
- openwakeword (wake word; onnx inference, no cloud / no access key)
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
- **Wake word**: Local detection with openWakeWord
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

### Integration Runner (CI)

`.github/workflows/integration-runner.yml` receives `repository_dispatch`
events of type `pr-integration` from participating service repos (v1:
`jarvis-command-center`). It runs `tests/test_loop_smoke.py`
against the dispatched PR's HEAD SHA using `tests/fakes/` to stand in for
`jarvis-llm-proxy-api` and `jarvis-whisper-api`, joins results to QA-plan
case IDs via `tools/parse_junit.py`, and posts a
`<!-- integration-test-results:v1 -->` comment + a `jarvis-integration`
commit status back on the originating PR.

QA-plan cases are bound to tests via `@pytest.mark.qa_case("CASE-NNN")` —
the marker is exported to JUnit XML by the hook in `tests/conftest.py`.
See [docs/integration-tests.md](docs/integration-tests.md) for the operator
guide (canned-response YAML, local reproduction, manual re-trigger, secrets).

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
