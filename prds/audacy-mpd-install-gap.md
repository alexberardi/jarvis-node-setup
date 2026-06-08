# Finding: Audacy install never enables mpd (silent break on reboot)

**Date:** 2026-06-07
**Discovered during:** Phase 2c (split-mode) soak validation on `jarvis-dev.local`
**Severity:** Medium — audacy command silently broken after the first reboot following install.
**Status:** FIXED 2026-06-07. See "What landed" below. Wrapper change still needs to reach the Pi for the install path to exercise the fix end-to-end.

## What landed

Three coordinated changes:

1. **Wrapper** (`jarvis-node-setup/scripts/jarvis-post-install`, `feat/wake-audio-split` branch — uncommitted on main):
   `_op_configure_systemd_service` now accepts `enable: bool`. When `true`,
   runs `systemctl enable <service>` between `daemon-reload` and `restart`.
   The idempotency check was extended via a new `_unit_is_enabled()` probe so
   re-running on a node where the drop-in matches but the unit was never
   enabled (the exact jarvis-dev recovery case) re-runs `enable` instead of
   no-op'ing. 6 new tests in `tests/test_post_install_wrapper.py`.

2. **SDK** (`jarvis-command-sdk/jarvis_command_sdk/forge.py` — uncommitted on main):
   `MANIFEST_SCHEMA["fields"]["post_install"]["item_fields"]` documents the
   `enable: bool` field for `configure_systemd_service`, so Forge LLM
   generates manifests that opt in correctly. Existing test
   `test_post_install_documents_configure_systemd_service_params` extended.

3. **Audacy package** (`jarvis-cmd-audacy`, **shipped as v0.1.2**):
   - `jarvis_package.yaml`: `enable: true` on the mpd op + version bump.
   - `audacy_shared/audacy_service.py`: new `mpd_alive()` thin wrapper over
     the existing `MPDClient.ping()`.
   - `commands/audacy/command.py:_handle_play`: probes `mpd_alive()` after
     station resolve, before the deferred-play setup. On failure returns a
     proper `error_response` so the user hears "mpd isn't running, run
     `sudo systemctl enable --now mpd`" instead of a confident "Playing
     X" followed by silence. This is option (3) from the original fix
     list — defense in depth, fires even if a future install regresses.

Pantry's `configure_systemd_service` allow-list already includes `mpd`
(added 2026-06-05); no change there. Pantry static_analysis doesn't
enforce strict-key validation on op fields, so existing manifests stay
accepted — the `enable` key is purely additive.

## Followup before this is fully end-to-end

The wrapper + SDK changes sit on `feat/wake-audio-split` (uncommitted) and
will land on main when that branch ships. Two ways to make a Pantry install
of audacy v0.1.2 actually exercise the new `enable` op on jarvis-dev:

- Sync the patched wrapper to `/usr/local/sbin/jarvis-post-install` on the
  Pi (root-owned, 0755) before installing, OR
- Run the workaround `sudo systemctl enable --now mpd` once on jarvis-dev
  to immediately fix the broken state. v0.1.2's defense-in-depth probe will
  also start surfacing a clean error if mpd ever dies again.

Either is fine; the workaround alone is enough to unblock the wake-audio-split
soak that originally found this.

## Symptom

User says "Play WFAN on Odyssey", wake fires normally, audacy command
pre-routes correctly, the TTS confirmation plays ("Playing Sports Radio 66
WFAN…"), and then **no audio comes out**. The main process logs:

```
2026-06-07 20:14:38,055 [ERROR] Deferred Audacy play failed | {
  'error': "I couldn't reach the stream for Sports Radio 66 WFAN.
            (mpd refused https://provisioning.streamtheworld.com/pls/WFANAAC.pls:
             [Errno 111] Connection refused)",
  'query': 'wfan on odyssey'
}
```

`Errno 111 ECONNREFUSED` from the local TCP connection to `localhost:6600`
(mpd's default control port). Audacy's `MPDClient` opens a fresh TCP
connection per operation; with mpd not running, every attempt refuses.

## Root cause chain

1. The audacy Pantry package drops a systemd unit drop-in at
   `/etc/systemd/system/mpd.service.d/jarvis.conf` with the comment
   `# managed-by: jarvis-package audacy`. Contents point mpd at
   `audacy_mpd.conf` and run it as `pi:audio` with the right
   `XDG_RUNTIME_DIR` / `PULSE_RUNTIME_PATH`.
2. **The package install never runs `systemctl enable mpd` or
   `systemctl start mpd`**. The drop-in alone has no effect — base
   `mpd.service` stays `disabled`.
3. After the next reboot, mpd never starts. `journalctl -u mpd` returns
   `-- No entries --` for the entire boot.
4. Audacy's `pre_route` happily synthesizes the TTS confirmation
   (`"Playing Sports Radio 66 WFAN…"`) because it doesn't probe mpd
   before generating the spoken response. The user hears the
   confirmation, doesn't realize playback failed.
5. Spotify works because go-librespot is a separate daemon launched
   by the spotify package's own keepalive loop (we see
   `go-librespot keepalive tick` every ~minute in main's log).

## Evidence

```
$ systemctl status mpd
○ mpd.service - Music Player Daemon
     Loaded: loaded (/usr/lib/systemd/system/mpd.service; disabled; preset: enabled)
    Drop-In: /etc/systemd/system/mpd.service.d/
             └─jarvis.conf
     Active: inactive (dead)

$ journalctl -u mpd --no-pager
-- No entries --

$ uptime -s
2026-06-06 21:00:31

$ cat /etc/systemd/system/mpd.service.d/jarvis.conf
# managed-by: jarvis-package audacy
[Unit]
Wants=user@1000.service
After=user@1000.service

[Service]
User=pi
Group=audio
Environment="MPDCONF=/home/pi/.jarvis/packages/audacy/audacy_lib/audacy_shared/audacy_mpd.conf"
Environment="XDG_RUNTIME_DIR=/run/user/1000"
Environment="PULSE_RUNTIME_PATH=/run/user/1000/pulse"
Restart=on-failure
RestartSec=5
```

## Why this stayed hidden

- mpd has been dead since the 2026-06-06 21:00 reboot — entire ~24 h
  of our soak/debug work today. Audacy never produced audio in that
  window; the only thing the user noticed was the TTS confirmation.
- Spotify produces obvious audio when it works, so its outage today
  (llm-proxy-down → TTS chain stalled) was loud. Audacy's failure mode
  is silence after a successful-sounding confirmation, which sounds
  identical to "I just missed the song" or "the speaker dipped".
- Phase 2c initially looked like the culprit because (a) the user's
  recent successful test was before the migration and (b) the IPC
  refactor touched the deferred-play hook (`on_response_complete`).
  Confirmed false: Phase 2c IPC delivered the command and fired the
  deferred-play callback correctly — we see audacy's own
  `ERROR Deferred Audacy play failed` log on the main side, originating
  from inside audacy's playback code, not from the IPC layer.

## Workaround (immediate)

```
sudo systemctl enable --now mpd
```

Survives reboot, starts mpd now. Audacy works again.

## Proper fix (audacy package)

Audit `~/.jarvis/packages/audacy/manifest.json` (or whichever the
Pantry install spec is) for the `post_install` operations. The
expected fix is one of:

1. Add an `enable_systemd_unit` op for `mpd.service` (auto-start on
   boot) alongside the existing unit-drop-in op.
2. Add a `start_systemd_unit` op for `mpd.service` so the install
   completes with mpd actually running (otherwise users have to
   reboot or manually start).
3. Add a `pre_route` mpd-liveness check in `command.py` that returns
   `PlaybackError("audacy isn't installed correctly — mpd not running")`
   instead of a spoken "Playing…" response. Defense in depth so a
   silent failure surfaces as an actionable message.

(3) is the most important — even if the package install gets fixed,
a stopped/crashed mpd should be detected and reported instead of
silently swallowing playback.

Where to look:

- `/home/pi/.jarvis/packages/audacy/` on the Pi (manifest, install ops)
- The Pantry package source repo (probably a `jarvis-cmd-audacy` git
  repo somewhere)
- Audacy's `command.py` `pre_route()` — needs an mpd-up probe before
  composing the spoken response

## Related — Spotify path is fine

Spotify's deferred-play handoff under split mode also works after the
Phase 2c IPC + the deferred-play callback fix landed on main side.
The `go-librespot` daemon is independently started/kept-alive by the
spotify command's `keepalive_tick` loop, so it doesn't depend on
systemd unit state. No package-install gap exists for spotify.

## Out of scope here

- The leak / RSS drift work tonight isn't gated on this. We can run the
  split-mode soak with mpd disabled; audacy just stays broken.
- A general "Pantry package post-install ops actually ran" verification
  story would be useful but is its own piece of work.
