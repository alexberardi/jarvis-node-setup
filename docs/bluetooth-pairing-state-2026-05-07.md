# Bluetooth Pairing — State of Play

Status as of 2026-05-07 EOD. Companion to `migration-to-pi-service.md` —
the user-switch from root → pi unblocked PulseAudio routing, but the
end-to-end pair → connect → audio flow on `jarvis-dev.local` is still
unreliable with Arctis GameBuds.

This doc is the handoff for a fresh-context session. It captures what
we changed, what we learned, what works, what doesn't, and where to
pick up.

## TL;DR

- **All migration patches landed and verified** — the User=pi flow is
  solid; logs, sudoers, OTA paths, AP-mode privilege escalation all
  good (see `migration-to-pi-service.md`).
- **BT scan is much better** — was always-2 noise, now finds 10–30
  named devices reliably with BR/EDR + LE pass + stale-cache prune.
- **BT pair has been substantially hardened**, but the Arctis
  GameBuds end-to-end path is still flaky. The flakiness is real and
  reproducible — it's a bluez ⇄ PipeWire ⇄ chip-firmware problem on
  Pi Zero 2W, not a missing patch.
- **Mobile UI gained Forget + Auto-reconnect controls**, staged but
  not yet built into the running app (no Expo reload yet).

## What's deployed where

### `jarvis-dev.local` (the Pi) — running

All node-side code changes are deployed and the service is running
under PID 4932 (started 2026-05-07 21:49:16 EDT). Files modified:

| File | What changed |
|------|--------------|
| `setup/jarvis-node.service` | Template uses `__SERVICE_USER__/__SERVICE_HOME__/__SERVICE_UID__` placeholders, adds `User=`, `Group=`, `XDG_RUNTIME_DIR=` |
| `install.sh` | `setup_service_user`, `migrate_to_pi_home`, `chown_install_dir`, `install_sudoers`, rollback safety, JARVIS_SECRET_DIRECTORY for alembic |
| `setup/pi.sh` | Updated sed call for new placeholders |
| `provisioning/wifi_manager.py` | `_priv()` helper wrapping 14 privileged AP-mode calls (`sudo -n` prefix) |
| `services/update_service.py` | STATE_FILE → `~/.jarvis/state/`, journal logging instead of /var/log file |
| `scripts/setup_mode.py` | `get_secret_dir()` instead of hardcoded `/root/.jarvis` |
| `core/platform_abstraction.py` | Major BT changes — see "BT changes" below |
| `services/bluetooth_scan_handler.py` | Pair flow: `remove → pair (auto-rescan) → trust → connect` |
| `scripts/mqtt_tts_listener.py` | Wired new `bluetooth-release` and `bluetooth-auto-connect` MQTT topics |

### `jarvis-command-center` (CC) — running, container restarted

| File | What changed |
|------|--------------|
| `app/api/bluetooth.py` | New routes: `POST /api/v0/nodes/{node_id}/bluetooth/release` and `.../bluetooth/auto-connect`. New Pydantic models `BluetoothReleaseBody` and `BluetoothAutoConnectBody`. Both call existing `_publish_bluetooth_mqtt()` helper. |

Verified live with `curl http://localhost:7703/openapi.json` — both
new paths show in the OpenAPI spec.

### `jarvis-node-mobile` — staged locally, NOT in the built app

| File | What changed |
|------|--------------|
| `src/api/bluetoothApi.ts` | Added `auto_connect?: boolean` to `BluetoothDevice`, exported `releaseBluetoothDevice()` and `setBluetoothAutoConnect()` helpers |
| `src/components/BluetoothSection.tsx` | Renders combined `Saved devices` section (connected + paired), each card has Auto-reconnect Switch + Disconnect (if connected) + Forget button with confirmation Dialog |

Need to reload Expo / rebuild for these to take effect on the device.

### Deleted

- `sync_files_to_zero.sh` — user said it's no longer used.

## BT changes — full detail

### `core/platform_abstraction.py`

**`PiBluetoothProvider.scan(timeout=30.0)`** — was 10s, only BR/EDR,
returned ghosts and MAC-as-name garbage.

Now:
- Runs `scan bredr` AND `scan le` passes within one bluetoothctl
  session (each gets `timeout/2`, 10s floor)
- Default 30s total (was 10s)
- `_prune_stale_cache()` removes non-paired devices from bluez before
  the scan so results reflect what's currently in range, not ghosts
- Filters out `[NEW]/[CHG]/[DEL]` event lines (only parses the final
  `devices` listing where the canonical name lives — earlier the
  RSSI: -52 line was being captured as the "name")
- Drops devices whose name is just a MAC string (bluez auto-fills
  MAC-as-name when the device doesn't broadcast one — useless to show)
- Strips ANSI color codes from bluetoothctl output

**`PiBluetoothProvider.pair(mac)`** — was a one-shot `bluetoothctl
pair` that failed with `AuthenticationFailed` on already-paired
devices.

Now:
- Pre-warms agent + pairable: `agent NoInputNoOutput`, `default-agent`,
  `pairable on` up front so timing isn't an issue
- Short-circuits `Paired: yes` → returns `True` immediately
- If device is unknown to bluez (`info` returns "not available", e.g.
  right after `remove`), runs `_scan_briefly()` to repopulate cache
  before issuing pair
- Otherwise issues normal `pair <MAC>`

**`PiBluetoothProvider.connect(mac)`** — was a 15s `bluetoothctl
connect` with no audio-profile verification.

Now:
- Reads `bluetoothctl info` once, classifies whether device is audio
- Short-circuits `Connected: yes` → **but still runs A2DP verification
  for audio devices** (the bug we hit at the very end — fresh pair
  leaves the device in `Connected: yes` state but A2DP hasn't
  negotiated yet; treating it as success leads to bluez dropping the
  bond ~30s later when AVDTP times out)
- Bumped main connect timeout to 30s (was 15s; cold connects can take
  20s+ on busy 2.4GHz)
- Detects `InProgress / br-connection-busy` and polls for in-flight
  connect to land instead of failing immediately
- Calls `_wait_for_audio_sink()` for audio devices — polls
  `pactl list sinks` AND `pactl list sources` for `bluez_sink.<MAC>`
  / `bluez_output.<MAC>` / `bluez_source.<MAC>` / `bluez_input.<MAC>`,
  requires the marker to be **stably present for 2 continuous seconds
  inside a 15s window**. Earlier I checked `pactl list cards`, but
  bluez registers the card on ACL link (before AVDTP), so the card
  was a false positive.
- If ACL succeeds but A2DP doesn't come up, explicitly `disconnect`
  the device so bluez stops the AVDTP retry storm (which is what was
  producing the audible "click" loop on the user's headphones)

### `services/bluetooth_scan_handler.py`

**`run_bluetooth_pair_and_upload`** — was `trust → pair → connect`
with no fallback. Then I tried optimistic `connect → fallback to
remove+pair+connect`, but the optimistic connect attempt itself
burned the buds' pairing-mode window (each attempt establishes ACL
briefly, which the buds interpret as "I'm connected, exit pairing
mode"). Ended at:

```
provider.remove(mac_address)        # no-op if not paired
provider.pair(mac_address)          # auto-rescans cache if needed
provider.trust(mac_address)
provider.connect(mac_address)       # full A2DP verification
```

Always treats the user's "Pair" tap as fresh-pair intent. Reconnect
of a paired-and-quiet device is the auto-reconnect loop's job, not
this handler's.

**`run_bluetooth_release(mac, forget=False)`** — new. Soft (default):
disconnect + flip `auto_connect=False`. Hard (forget=True): also
calls `provider.remove()` + deletes JarvisStorage record.

**`run_bluetooth_set_auto_connect(mac, enabled)`** — new. Toggles the
per-device auto-reconnect flag in JarvisStorage.

**`_persist_device`** — preserves the user's existing `auto_connect`
choice across re-pairs (was overwriting to `True` every time, breaking
the "I turned this off, why is it back on?" flow). Falls back to
`bluetoothctl info` to get a real device name when
`get_paired_devices()` doesn't return it (was silently using MAC as
name).

### `scripts/mqtt_tts_listener.py`

Two new topic handlers + dispatch entries:
- `jarvis/nodes/<id>/bluetooth-release`
- `jarvis/nodes/<id>/bluetooth-auto-connect`

The wildcard subscription `jarvis/nodes/<node_id>/#` already covers
these — only the `on_message` dispatch needed updating.

## Failure mode taxonomy — what we learned

These are the distinct failure modes we hit, in order of nastiness:

### 1. Already-paired device fails to re-pair with `AuthenticationFailed`

**Cause:** `bluetoothctl pair` on an already-bonded peer triggers a
fresh handshake that the peer (Arctis) rejects with bluez's
`org.bluez.Error.AuthenticationFailed`. Most peripherals only accept
fresh-pair handshakes when *they* are in pairing mode, not when an
already-bonded peer initiates one.

**Fix:** `pair()` short-circuits on `Paired: yes`. ✅

### 2. ACL connects but A2DP profile silently fails

**Cause:** `bluetoothctl connect` returns success when the BR/EDR ACL
link is up. A2DP/HFP profiles negotiate over AVDTP afterwards, and
can time out without bluez surfacing it via the `connect` return
code. When this happens, bluez logs `Discover: Connection timed out`
and retries every ~10s — each retry produces an audible "tick" on the
headphones (the original "click loop" symptom). Eventually bluez
gives up and either disconnects cleanly OR drops the bond entirely.

**Fix:** `connect()` polls `pactl list sinks/sources` for a
`bluez_sink/output/source/input.<MAC>` to be stably present for 2s
within 15s. If absent, explicitly disconnect to stop bluez's retry
storm. ✅

### 3. Bond evaporates after silent A2DP failure

**Cause:** When AVDTP keeps failing and bluez decides to drop the
link, *the bond itself* sometimes gets cleared too — the device
goes from `Paired: yes / Bonded: yes` to `Paired: no / Bonded: no /
Trusted: yes` (the trust flag is orphaned). Sometime in
bluez ≥ 5.80 this became more aggressive.

**Fix:** Only partial. We disconnect cleanly when A2DP fails (which
prevents the worst retry-loop), and the pair handler always does a
fresh pair on user tap (which re-bonds even when the bond is gone).
But we can't prevent bluez from clearing the bond once it has
decided to. ⚠️

### 4. Connect-first attempt burns the peer's pairing-mode window

**Cause:** When the user puts buds in pairing mode and taps "Pair",
the buds advertise for a limited window (typically 60–120s for
Arctis). If our pair flow tries `connect` first (assuming the bond
still works) and the buds reject because-they're-in-pairing-mode,
that brief ACL handshake + the 15s A2DP wait still tells the buds
"someone connected to you, exit pairing mode". By the time we fall
back to fresh pair, the buds have stopped advertising and the
rescan can't find them.

**Fix:** Pair handler **never** tries connect first. Always goes
straight to `remove → pair → trust → connect`. The user's tap is
fresh-pair intent; reconnect is the auto-reconnect loop's job. ✅

### 5. `bluetoothctl pair <MAC>` requires the device to be in bluez's cache

**Cause:** After `bluetoothctl remove <MAC>`, bluez has no record of
the device. `pair <MAC>` returns "Device not available" until a
discovery (scan) repopulates the cache.

**Fix:** `pair()` detects this and runs `_scan_briefly()` (max 12s)
to rediscover the MAC before issuing pair. ✅

### 6. Pi Zero 2W BT chipset (BCM43430A1) limitations

**Cause:** The chip has weak concurrent-profile support compared to
modern hardware. PipeWire's bluez5 module sometimes wins races
against bluez itself for the audio fd. When that happens, AVDTP
gets `Device or resource busy` even when no other host is grabbing
the device.

**Fix:** None — this is a hardware limitation. Suspect this is the
root cause of why end-to-end is still flaky even with everything
above fixed. Detection (failure mode #2) makes the failure clean,
but it doesn't make it succeed.

### 7. Arctis GameBuds-specific quirks

- Aggressive power management — exits pairing mode within ~60s,
  refuses connects when in standby
- Single-A2DP-master limitation — if bonded to phone with phone BT
  on, Pi can't grab audio profile
- "Helpful" auto-exit-pairing-mode-when-anything-connects behavior
  conflicts with our connect-first-then-fallback pattern (fixed in
  failure mode #4)

## Where we ended

Last meaningful event sequence (PID 4477, before the latest deploy):

```
21:46:38  pair requested
21:46:38  fallback: rescanning briefly
21:46:45  Bluetooth device paired           ← fresh pair worked
21:46:45  Bluetooth device trusted
21:46:46  Bluetooth device already connected ← treated as success
21:46:46  Bluetooth pair complete | 'Arctis GameBuds'
```

Then within ~30s, AVDTP timed out behind the scenes, bluez cleared
the bond, and the buds went to standby. By the time we checked,
state was `Paired: no / Connected: no / Trusted: yes`.

This was the bug that motivated the LAST patch (in PID 4932): the
"already connected" short-circuit in `connect()` now also runs A2DP
verification. We deployed that patch but didn't get a clean test of
it because the user's mobile UI started pinwheeling and the buds
exited pairing mode again before the next attempt fired.

## Open puzzles

1. **Why does bluez clear the bond after AVDTP failure?** This is
   not standard behavior — bonds should persist regardless of
   profile-level failures. Possibly a bluez 5.82-on-Bookworm bug,
   possibly a chip-firmware ACL timeout that's interpreted as a
   bond reset. Worth filing a bluez issue OR pinning to an older
   bluez version.
2. **Why is MQTT flapping every 1–2 minutes?** Logs show
   `MQTT_ERR_CONN_LOST` and `MQTT_ERR_KEEPALIVE` (16) periodically.
   May or may not be related to BT; worth confirming the broker on
   the laptop is healthy. If unrelated, just noise to ignore.
3. **Did the fresh-pair attempt with PID 4932 (the latest deploy)
   actually exercise the new connect-with-A2DP-on-already-connected
   path?** Mobile UI was pinwheeling — `bluetooth-pair` MQTT may
   have never reached the node. Worth confirming with one clean
   end-to-end test.

## Suggested next-session starting points

In order of effort vs payoff:

### Cheap (next session, 30 min)

- Fresh-context test: power-cycle the Arctis (full reset if possible),
  verify the latest code path works end-to-end. Specifically watch
  for the new "ACL up but audio profile failed to negotiate" log
  message — if you see it, the new check is firing correctly.
- Verify the mobile UI Forget + Auto-reconnect work after Expo reload.
- Add `BluetoothStatusResponse.auto_connect` so the mobile toggle
  reflects real state instead of optimistic local state.

### Medium (a few hours)

- Have CC's `_send_bluetooth_deep_link_push` show *what failed* in the
  inbox notification ("audio profile failed — peer may be bonded
  elsewhere") so users get diagnostic feedback without needing logs.
- Add a "Reconnect" button (separate from Pair) for already-paired
  devices in `Saved devices`. Right now there's no UI affordance for
  reconnecting a `Paired-but-disconnected` device — the auto-reconnect
  loop handles it eventually but the user has no manual button.
- Try `bluez 5.84` (newer than Bookworm's 5.82) via Debian backports
  to see if the bond-evaporation behavior is fixed.

### Heavy (a day+)

- **Drop bluez+PipeWire, try bluealsa.** ALSA-level BT stack, simpler.
  Less feature-rich (only one device at a time, no simultaneous TTS-to-
  HifiBerry + music-to-BT) but possibly more reliable for the headphone
  use case. The migration doc explicitly considered this and rejected
  it for the dual-output requirement; revisit if the dual-output
  isn't paying for the complexity.
- **Move BT into a privileged helper daemon.** Single component owns
  the bluetoothctl session, exposes IPC commands. Eliminates timing
  races between the various pair/connect calls. Lots of work.
- **Test on a non-Arctis device.** A known-good speaker or a different
  pair of headphones. If THOSE pair cleanly with the current code,
  Arctis is the bottleneck and we can document that limitation. If
  they DON'T pair cleanly, we have systemic stack issues to chase.

## Touchstones for verifying things work

When trying again next session:

- `ssh pi@jarvis-dev.local 'bluetoothctl info <MAC>'` — should show
  `Paired: yes`, `Bonded: yes`, `Connected: yes`, `Trusted: yes` for
  a fully working bond.
- `ssh pi@jarvis-dev.local 'pactl list sinks short'` — should show a
  `bluez_output.<MAC>` line when the audio profile is up. Default
  sink (`pactl get-default-sink`) should auto-switch to it via
  PipeWire's `module-switch-on-connect`.
- Watch for these log lines in `journalctl -u jarvis-node -f`:
  - `Bluetooth device paired` — pair handshake succeeded
  - `Bluetooth device connected` — full success including A2DP
    verification
  - `ACL connected but audio profile failed to negotiate` — partial
    failure detected, clean disconnect happens automatically
- Spotify (configured in `/home/pi/.jarvis/spotify/spotifyd.conf` with
  `backend = "pulseaudio"` and no explicit device) follows the
  default sink — so when BT is up and the default switches to
  `bluez_output`, Spotify routes there automatically.

## Related files for next session

- `migration-to-pi-service.md` — the User=pi → root migration that
  preceded this work
- `core/platform_abstraction.py` — all the Pi BT code
- `services/bluetooth_scan_handler.py` — the MQTT pair handler
- `scripts/main.py:303-345` — the auto-reconnect loop (10-min
  interval, runs `provider.connect()` for each saved device with
  `auto_connect: True`)
- `services/storage_backend.py` — JarvisStorage backend that the
  pair flow writes to
