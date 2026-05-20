# Migration: jarvis-node service from `root` to `pi`

Status: **dev hand-patched on `jarvis-dev.local`** (2026-05-06). Installer changes still pending — this doc is the spec.

## Why

Bluetooth audio routing requires PulseAudio. PulseAudio (and its PipeWire-pulse compatibility shim on Bookworm) runs per-user with its socket at `/run/user/<uid>/pulse/native`. PulseAudio actively rejects root connections to a user session:

```
$ sudo XDG_RUNTIME_DIR=/run/user/1000 pactl info
XDG_RUNTIME_DIR (/run/user/1000) is not owned by us (uid 0), but by uid 1000!
(This could e.g. happen if you try to connect to a non-root PulseAudio as a root
 user, over the native protocol. Don't do that.)
Connection failure: Access denied
```

Because the SDK's `BluetoothAudio.get_sink()` shells out to `pactl list sinks` to find the `bluez_sink.*` PCM, running as root means the call always returns `None`, no `PULSE_SINK` env var is set, and audio always falls through to the local HifiBerry — never the BT speaker.

Alternatives we considered:
- **bluealsa** (`bluez-alsa-utils`) — ALSA-level BT plugin. Lets root route to BT, but adds a new daemon, conflicts with PulseAudio's bluez module, and exposes only one device at a time (loses the "TTS local + music BT" split).
- **PulseAudio system mode** — deprecated, fragile, security headache.
- **Run jarvis-node as `pi`** — clean, matches the original SDK design. **Chosen.**

## What changed (on the running Pi `jarvis-dev.local`)

1. `pi` added to `bluetooth` group: `sudo usermod -aG bluetooth pi`
2. State migrated: `cp -a /root/.jarvis /home/pi/.jarvis` then `chown -R pi:pi /home/pi/.jarvis`
3. Install dir chowned: `chown -R pi:pi /opt/jarvis-node`
4. Service unit `/etc/systemd/system/jarvis-node.service` rewritten with:
   ```
   User=pi
   Group=pi
   Environment=HOME=/home/pi
   Environment=XDG_RUNTIME_DIR=/run/user/1000
   ```
5. `systemctl daemon-reload && systemctl restart jarvis-node`
6. Backup of pre-migration state at `/tmp/jarvis-pre-pi-migration-1778117177.tar.gz` (contents: old `/root/.jarvis` + old service unit).

`XDG_RUNTIME_DIR` is critical — systemd does NOT propagate the user's runtime dir to system services even with `User=`. Without it, libpulse can't find the socket.

## Installer changes needed for the next release

### 1. Service template (`setup/jarvis-node.service`)

Add `User`/`Group`, add `XDG_RUNTIME_DIR`, replace `__HOME__` with `__SERVICE_HOME__`. Keep placeholders so a non-default user/UID still works:

```ini
[Service]
User=__SERVICE_USER__
Group=__SERVICE_USER__
ExecStart=__VENV__/bin/python -m scripts.main
WorkingDirectory=__PROJECT_DIR__
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=300
TimeoutStopSec=30
KillSignal=SIGTERM
Environment=HOME=__SERVICE_HOME__
Environment=XDG_RUNTIME_DIR=/run/user/__SERVICE_UID__
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=__PROJECT_DIR__
Environment=CONFIG_PATH=__PROJECT_DIR__/config.json
SyslogIdentifier=jarvis-node
```

### 2. `install.sh` — three new functions

```bash
SERVICE_USER="pi"
SERVICE_HOME="/home/pi"

setup_service_user() {
  # Idempotent: usermod is a no-op if already in group, lingering is one-shot.
  usermod -aG bluetooth "$SERVICE_USER"
  loginctl enable-linger "$SERVICE_USER"
}

migrate_to_pi_home() {
  # Only migrate when the source has provisioning state and the target doesn't —
  # avoids clobbering a real /home/pi/.jarvis on subsequent runs.
  if [ -f /root/.jarvis/.provisioned ] && [ ! -f "${SERVICE_HOME}/.jarvis/.provisioned" ]; then
    info "Migrating /root/.jarvis → ${SERVICE_HOME}/.jarvis"
    mkdir -p "${SERVICE_HOME}/.jarvis"
    cp -a /root/.jarvis/. "${SERVICE_HOME}/.jarvis/"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${SERVICE_HOME}/.jarvis"
    # Leave /root/.jarvis as a backup; later releases can prune.
  fi
}

chown_install_dir() {
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
}
```

### 3. `install.sh` — updated `create_service()`

Pass the new placeholders to `sed`:

```bash
SERVICE_UID="$(id -u "${SERVICE_USER}")"
sed -e "s|__VENV__|${INSTALL_DIR}/.venv|g" \
    -e "s|__PROJECT_DIR__|${INSTALL_DIR}|g" \
    -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
    -e "s|__SERVICE_HOME__|${SERVICE_HOME}|g" \
    -e "s|__SERVICE_UID__|${SERVICE_UID}|g" \
    "${INSTALL_DIR}/setup/jarvis-node.service" \
    > "/etc/systemd/system/${SERVICE_NAME}.service"
```

The fallback inline service block (when the template is missing) needs the same updates.

### 4. `install.sh` — updated `main()` order

```
preflight
get_version
install_apt_deps
download_and_extract
configure_audio
setup_config
rebuild_venv
setup_database
register_commands
setup_service_user      # NEW
migrate_to_pi_home      # NEW
chown_install_dir       # NEW
create_service          # uses new template
start_service
```

`setup_service_user` runs before `migrate_to_pi_home` so lingering is enabled before the service starts (without lingering, `/run/user/1000` doesn't exist on boot until pi logs in, and the service races on startup).

`chown_install_dir` runs at the end so we don't fight ownership during the heavy steps (download/extract/pip).

### 5. Bundle (already-staged uncommitted changes)

These all want to ship in the same release for consistency:

- `services/bluetooth_scan_handler.py` (new) — handles MQTT scan/pair/disconnect/discoverable from CC
- `scripts/mqtt_tts_listener.py` — wires the four `bluetooth-*` MQTT topics to the handlers
- `core/platform_abstraction.py` — `PiBluetoothProvider.scan()` rewritten with `bluetoothctl` Popen + `scan bredr` so audio devices actually appear
- `install.sh` — apt list adds `pulseaudio` + `pulseaudio-module-bluetooth` (already done in working tree)
- `setup/pi.sh` — same apt addition (legacy script, kept in sync)
- `core/version.py` + `config.example.json` — `release_track` field (separate concern, neutral, keep)

## Existing-node migration (what `migrate_to_pi_home` handles)

Files in `/root/.jarvis/` to preserve:

| File | Purpose |
|------|---------|
| `db.key` | Fernet master key for the encrypted SQLite DB |
| `secrets.key` | Legacy Fernet key (older nodes) |
| `k2.enc` | Settings encryption key (shared with mobile via QR) |
| `k2_metadata.json` | K2 key ID and creation timestamp |
| `wifi_credentials.enc` | Saved WiFi creds for reboot reconnection |
| `.provisioned` | Marker indicating provisioning is complete |
| `packages/` | Pantry-installed package metadata + shared code (`<pkg>/lib/`, `<pkg>.json`) |
| `cache/` | Misc runtime caches |

`cp -a` preserves `chmod 600` on the key files; the recursive `chown` then flips ownership to `pi:pi`. Permissions are correct for the pi user to read/write.

## Testing checklist

### Phase 1 — known-risky paths (do these first)

- [ ] **Provisioning AP mode (FRESH INSTALL)**. hostapd + dnsmasq need root. Spin up a clean Pi, run `install.sh`, see if AP mode comes up. If not, follow-up needed: polkit rule or sudoers exception so `pi` can `systemctl start hostapd dnsmasq`. **This is the most likely failure point.**
- [ ] **Factory reset**. The reset flow calls `reboot` / `shutdown`. Pi is in the `sudo` group but typically `sudo` requires a password. Check whether the existing path uses `systemctl reboot` (works for any user with appropriate polkit) or shells out to `sudo reboot`.
- [ ] **WiFi credential write during provisioning** (mobile-driven). nmcli should work via `netdev` group, but if provisioning writes directly to `/etc/wpa_supplicant/wpa_supplicant.conf` it'll fail.
- [ ] **bluetoothctl pair / trust / connect** triggered from the running service. Read-only `bluetoothctl info` works. Pair/trust go through D-Bus methods with stricter polkit policies — verify with the mobile Hardware tab pairing flow.

### Phase 2 — file ownership / state correctness

- [ ] **Pantry install of a new package** end-to-end (mobile → CC → MQTT → node). `pip install` writes into `/opt/jarvis-node/.venv` which is now `pi`-owned.
- [ ] **Database writes under load** — fire several reminders, check they persist.
- [ ] **Settings snapshot upload** — the K2 encryption read from `/home/pi/.jarvis/k2.enc` and snapshot upload via REST.
- [ ] **Encrypted secrets read/write** — set a new secret via the mobile app, confirm it reads back.

### Phase 3 — end-to-end voice loop

- [ ] Wake word detection (pyaudio capture; pi has `audio` group)
- [ ] Full voice loop: mic → STT → CC → command → TTS → speaker
- [ ] Each previously-installed command at least once: calculator, weather, news, calendar, timer, reminder, control_device, pandora
- [ ] BT audio: pair Arctis via mobile Hardware tab → play pandora → audio routes to BT
- [ ] TTS plays through local HifiBerry (NOT BT) when BT is paired — the "music to BT, voice to local" split

### Phase 4 — observability

- [ ] Heartbeat to CC continues
- [ ] Logs reach `jarvis-logs` (we saw `HTTP 403` errors during the migration session — may be unrelated to user switch, but worth checking the app credentials work as `pi`)

## Rollback

Two safety nets exist on `jarvis-dev.local`:

1. **Pre-migration tarball**: `/tmp/jarvis-pre-pi-migration-1778117177.tar.gz` — old `/root/.jarvis/` + old service unit. To restore:
   ```bash
   sudo systemctl stop jarvis-node
   sudo tar -xzf /tmp/jarvis-pre-pi-migration-*.tar.gz -C /
   sudo chown -R root:root /opt/jarvis-node
   sudo systemctl daemon-reload
   sudo systemctl restart jarvis-node
   ```
   Will lose any state changes made since the migration (Pantry installs, secrets, settings).

2. **Installer's own rollback** (`.bak` directory) — `install.sh` keeps `${INSTALL_DIR}.bak` after each upgrade and auto-rolls back if the new service doesn't become active within 120s. This needs verifying still works after the user-switch (the rollback will restore the old root-mode unit too).

## Out of scope (Phase 2 followups)

These are flagged but NOT being addressed in this migration:

- **Polkit/sudoers for system operations** — if Phase 1 testing finds AP mode / factory reset / etc. broken as `pi`, write polkit rules or a tight `sudoers.d/jarvis` entry granting `NOPASSWD` for specific binaries (`hostapd`, `dnsmasq`, `reboot`, `shutdown`).
- **PulseAudio `module-switch-on-connect` persistence** — already loaded at runtime + persisted via `~/.config/pipewire/pipewire-pulse.conf.d/20-jarvis-bt-sticky.conf`, but only on the dev Pi. Decision deferred per the "explicit pair mechanic" preference for the BT-speaker UX.
- **bluez `AutoEnable=true`** — would help BT devices reconnect on power-up, but only useful for the headphones-style use case (not the party-speaker use case).

## Related memory

- [`project_pi_user_model.md`](../../../.claude/projects/-Users-alexanderberardi-jarvis-jarvis-node-setup/memory/project_pi_user_model.md) — root cause + decision rationale (auto-loaded each session)
- [`project_pandora_runaway_loop.md`](../../../.claude/projects/-Users-alexanderberardi-jarvis-jarvis-node-setup/memory/project_pandora_runaway_loop.md) — companion: the player loop bug that masked all of this until BT routing was investigated
