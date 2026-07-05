"""PulseAudio-based volume / mute control for the ReSpeaker 2-Mics Pi HAT v2.

Used by:
- mqtt_tts_listener.handle_update_node_config — when the mobile app
  pushes a ``volume_percent`` or ``is_muted`` key.
- commands.control_node_command — voice intents ("volume up", "set
  volume to 5", "mute", ...).
- settings_snapshot_service.build_snapshot — current volume + detected
  audio card in the snapshot so the mobile slider and Hardware tab
  reflect reality.

User-facing volume control flows through PulseAudio (``pactl
set-sink-volume`` / ``set-sink-mute``). The codec mixer baseline (TLV320
PCM at full, Line at +4 dB, Line DAC at -1.5 dB, HP/HPCOM muted) is set
by install.sh and re-asserted on every node startup by
``ensure_output_baseline()`` — the alsactl-restore round-trip isn't
reliable enough to trust for the output mixer either, mirroring
``ensure_adc_hpf_enabled()`` for the input HPF.

Why two sink classes (@DEFAULT_SINK@ + bluez_sink.*)?
@DEFAULT_SINK@ covers TTS, chimes, and music routed to the on-board
JST speaker. bluez_sink.* covers anything routed to a paired Bluetooth
speaker (PA exposes those as separate sinks). Setting both keeps every
audio path coherent regardless of where the user has pointed playback.
"""

import json
import os
import re
import subprocess
from functools import lru_cache

from jarvis_log_client import JarvisLogger

from utils.config_file import update_config_file

logger = JarvisLogger(service="jarvis-node")


def _run(cmd: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess | None:
    """Run a shell command, returning None when the binary is missing or times out."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"{cmd[0]} unavailable", error=str(e))
        return None


def _alsa_sink_suspended() -> bool:
    """True if any sink driven by ``module-alsa-card`` is in SUSPENDED state."""
    sinks = _run(["pactl", "list", "short", "sinks"], timeout=1.0)
    if sinks is None or sinks.returncode != 0:
        return False
    # Each line: <id>\t<name>\t<driver>\t<format>\t<state>
    for line in sinks.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 5 and fields[2] == "module-alsa-card.c" and fields[4] == "SUSPENDED":
            return True
    return False


def _reload_alsa_card_module(reason: str) -> bool:
    """Unload + reload module-alsa-card with its current args. Returns True on success.

    This is the only sequence that reliably wakes a TLV320 sink that has
    fallen into SUSPENDED — ``pactl suspend-sink ... 0`` returns "Invalid
    argument" on this hardware. Destroying and recreating the sink clears
    every internal suspend_cause bit.
    """
    modules_short = _run(["pactl", "list", "short", "modules"], timeout=1.0)
    if modules_short is None or modules_short.returncode != 0:
        return False
    alsa_card_id: str | None = None
    for line in modules_short.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[1] == "module-alsa-card":
            alsa_card_id = fields[0]
            break
    if alsa_card_id is None:
        return False

    modules_full = _run(["pactl", "list", "modules"], timeout=2.0)
    if modules_full is None or modules_full.returncode != 0:
        return False
    args: str | None = None
    in_target = False
    for line in modules_full.stdout.splitlines():
        if line.startswith(f"Module #{alsa_card_id}"):
            in_target = True
            continue
        if in_target and line.startswith("Module #"):
            break
        if in_target:
            stripped = line.lstrip()
            if stripped.startswith("Argument:"):
                args = stripped[len("Argument:"):].strip()
                break
    if not args:
        return False

    logger.warning(
        "Reloading module-alsa-card",
        alsa_card_id=alsa_card_id,
        reason=reason,
    )
    _run(["pactl", "unload-module", alsa_card_id], timeout=2.0)
    # Give pulse/ALSA a moment to release the underlying PCM device.
    # Without this brief settle, the immediate reload sometimes lands on
    # a half-closed kernel device and the new sink comes up in a state
    # where subsequent sink-input attaches trigger "Resume failed,
    # couldn't restore original sample settings." Manual recovery
    # (unload + sleep 1 + load) always works — match that pacing.
    import time as _time_mod
    _time_mod.sleep(0.6)
    # The Argument string contains quoted values (e.g. card_properties=
    # "module-udev-detect.discovered=1") — shell=True is required so the
    # shell parses it as a word list with proper quoting. If the load fails
    # the card stays absent until pulse restart; udev-detect won't re-add
    # it absent a fresh udev event, so we log loudly on failure.
    try:
        result = subprocess.run(
            f"pactl load-module module-alsa-card {args}",
            shell=True, capture_output=True, text=True, timeout=3.0,
        )
        if result.returncode != 0:
            logger.error(
                "alsa-card reload failed; sink will remain absent until pulse restart",
                stderr=result.stderr.strip(),
            )
            return False
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.error("pactl reload alsa-card raised", error=str(e))
        return False
    return True


def reload_alsa_card_if_suspended() -> bool:
    """Recover a SUSPENDED PA sink by unloading + reloading module-alsa-card.

    On the TLV320AIC3104 (ReSpeaker v2 HAT) the sink frequently ends up in
    SUSPENDED state after a long wake-to-TTS gap. aplay then attaches a
    sink-input but the sink never drains — aplay blocks in the kernel
    until killed and jarvis-node stays in "audio playing" state forever.

    Cheap (~25ms ``pactl list short sinks``) when the sink is healthy, so
    safe to call inline before each TTS playback. The recovery itself
    takes ~500ms but only runs when the sink is actually wedged.

    Returns True if a reload was needed and attempted; False if the sink
    was already healthy or pactl is unavailable. Never raises.
    """
    if not has_respeaker_hat():
        return False
    if not _alsa_sink_suspended():
        return False
    return _reload_alsa_card_module(reason="sink SUSPENDED")


def force_reload_alsa_card() -> bool:
    """Unconditionally reload module-alsa-card.

    Use ONLY at moments where we know the sink is about to be attached to
    by a freshly-spawned consumer (notably ``on_response_complete`` for
    deferred media playback). The TLV320 driver's resume-from-SUSPENDED
    path is broken — pulse logs "Resume failed, couldn't restore original
    sample settings" the moment librespot/MA/mpv creates its sink-input.

    Checking-then-reloading misses the gap: between TTS aplay exiting and
    the deferred player attaching, the sink can be IDLE (not SUSPENDED
    yet) when we check, then go SUSPENDED a moment later. By the time the
    deferred play fires it's too late to recover from the new sink-input
    side.

    ~500ms cost. Only call from the deferred-play hook, NOT inline before
    every aplay (use reload_alsa_card_if_suspended for that).
    """
    if not has_respeaker_hat():
        return False
    return _reload_alsa_card_module(reason="pre-deferred-play")


# ── Audio card discovery (diagnostic only) ──────────────────────────────────

# Pi's built-in HDMI audio device — present on every Pi whether a screen
# is attached or not. The on-board speaker we care about is always the
# next non-HDMI card.
_HDMI_CARD_PREFIX = "vc4"


def get_audio_card() -> str | None:
    """Return the on-board audio card name (e.g. ``seeed2micvoicec``).

    Diagnostic-only — surfaces in the settings snapshot so the mobile
    Hardware tab can show what the node detected. Returns None when no
    non-HDMI card is present (e.g. macOS dev node, plain Pi with no HAT).
    """
    result = _run(["aplay", "-l"])
    if result is None or result.returncode != 0:
        return None
    for match in re.finditer(r"^card\s+\d+:\s+(\S+)", result.stdout, re.MULTILINE):
        name = match.group(1)
        if not name.startswith(_HDMI_CARD_PREFIX):
            return name
    return None


@lru_cache(maxsize=1)
def has_respeaker_hat() -> bool:
    """True if the ReSpeaker 2-Mic HAT (TLV320AIC3104, ALSA card
    ``seeed2micvoicec``) is present.

    The SUSPENDED-sink reload, the output-mixer baseline, the ADC-HPF fix and
    the sink keepalive all exist solely to work around that codec's driver
    bugs. They must not run on other hardware (USB audio, a container sharing
    a host sink, macOS), where they're useless at best and disruptive at
    worst (e.g. unloading the *host's* alsa-card module from inside a
    container). Cached: the hardware doesn't change at runtime.
    """
    # In a container — or any host that sets JARVIS_NODE_OS to a non-Pi
    # value — we don't own the kernel module / PulseAudio daemon, even if the
    # HAT card is visible via /dev/snd passthrough. Only a native Pi node (no
    # override, or an explicit JARVIS_NODE_OS=PI) should run these workarounds.
    override = os.getenv("JARVIS_NODE_OS", "").upper()
    if override and override != "PI":
        return False
    return get_audio_card() == "seeed2micvoicec"


# ── Codec self-heal (TLV320AIC3104 ADC HPF) ─────────────────────────────────

# The TLV320AIC3104's internal ADC high-pass filter has four settings:
# Disabled (0), 0.0045xFs (1), 0.0125xFs (2), 0.025xFs (3). The codec
# powers on with the filter disabled — meaning every captured sample
# carries the analog input's DC component, which produced the May-2026
# beta-blocker where Whisper transcribed kitchen voice commands as
# "*sad music*" (the DC pedestal looked like a sustained low-frequency
# drone to the speech model).
#
# Item 1 (0.0045xFs ≈ 216 Hz at 48 kHz) is the gentlest setting that
# still blocks DC; speech intelligibility is unaffected since voice
# energy below 216 Hz is mostly fundamental rumble that telephone-band
# wake-word + STT models discard anyway.
#
# install.sh applies this at install time AND alsactl-stores it, but
# alsactl is unreliable for ENUMERATED controls across kernel updates,
# so the node also runs this check on every startup as a self-heal.

_TLV320_ADC_HPF_CONTROL = "ADC HPF Cut-off"
_TLV320_ADC_HPF_TARGET_ITEM = 1  # 0.0045xFs


def _get_adc_hpf_setting(card: str) -> int | None:
    """Return the current ADC HPF item index (0-3), or None if unreadable."""
    result = _run([
        "amixer", "-c", card, "cget", f"name={_TLV320_ADC_HPF_CONTROL}",
    ])
    if result is None or result.returncode != 0:
        return None
    # cget output ends with a line like ``  : values=0,0`` — both channels
    # share a value on this codec, so reading either is fine. Use a regex
    # to be lenient about whitespace + comma variations.
    match = re.search(r"^\s*:\s*values=(\d+)", result.stdout, re.MULTILINE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def ensure_adc_hpf_enabled() -> None:
    """If the TLV320AIC3104 is present and its ADC HPF is disabled, enable it.

    Idempotent and safe to call on every startup. No-op when the codec
    is missing (macOS dev nodes, Pi without the ReSpeaker HAT) or the
    HPF is already at a non-disabled item. Logs the action it takes.
    """
    card = get_audio_card()
    if card != "seeed2micvoicec":
        return
    current = _get_adc_hpf_setting(card)
    if current is None:
        logger.info(
            "ADC HPF state unreadable, skipping self-heal (codec may be busy)",
            card=card,
        )
        return
    if current >= 1:
        # Already enabled at a usable cutoff. Don't downgrade higher
        # cutoffs that a user might have deliberately picked.
        return
    logger.warning(
        "ADC HPF was disabled — enabling to remove DC bias from recordings",
        card=card,
        previous_item=current,
        target_item=_TLV320_ADC_HPF_TARGET_ITEM,
    )
    result = _run([
        "amixer", "-c", card, "cset",
        f"name={_TLV320_ADC_HPF_CONTROL}",
        str(_TLV320_ADC_HPF_TARGET_ITEM),
    ])
    if result is None or result.returncode != 0:
        logger.error(
            "amixer cset for ADC HPF failed",
            stderr=(result.stderr if result else None),
        )


# ── Codec self-heal (TLV320AIC3104 output mixer baseline) ───────────────────

# install.sh sets the codec's output mixer to a calibrated baseline at
# install time (PCM at 0 dB, Line at +4 dB, Line DAC at -1.5 dB, HP/HPCOM
# muted — see install.sh's configure_audio() for the full notes), and
# alsactl-stores it so alsa-restore replays the values on every boot.
#
# Two failure modes still leave a node sitting at the codec's quiet
# power-on defaults — ~26 dB below the calibrated level, user-perceived
# as "100% volume is barely audible":
#
#   1. Fresh install: install.sh adds the dt-overlay to config.txt but
#      the codec module isn't loaded until the post-install reboot, so
#      configure_audio's amixer block sees no card and silently skips
#      (logged but not fatal). install.sh's tail says "mixer baseline
#      applies on next install run" — but most users never re-run it.
#
#   2. alsactl-restore round-trip: the kernel TLV320 driver doesn't
#      always replay every control value cleanly. The May-2026 prod
#      kitchen calibration session that motivated jarvis-alsa-store also
#      saw the live mixer state diverge from what asound.state held after
#      a boot — same root cause as the ADC HPF case above.
#
# Re-asserting at every node startup is cheap insurance, symmetric with
# the ADC HPF self-heal. The persist step uses /usr/local/sbin/jarvis-
# alsa-store (NOPASSWD via /etc/sudoers.d/jarvis-node) so the heal also
# fixes asound.state for subsequent boots — without it the heal would
# re-fire forever.

# Target values mirror install.sh's mixer baseline block.
_TLV320_OUTPUT_BASELINE_CMDS: tuple[tuple[str, ...], ...] = (
    ("PCM", "100%", "unmute"),
    ("Line", "2", "unmute"),
    ("Line DAC", "115", "unmute"),
    ("Left Line Mixer DACL1", "on"),
    ("Right Line Mixer DACR1", "on"),
    ("HP", "0", "mute"),
    ("HP DAC", "0", "mute"),
    ("HPCOM", "0", "mute"),
    ("HPCOM DAC", "0", "mute"),
)

# Drift thresholds. Line baseline is +2 dB (control value 2), tuned down
# from +4 dB on 2026-06-03 — the v0.1.100 sink keepalive caused multi-
# stream mixing to push the codec analog stage past its clip point at
# high volume; +2 dB gives 2 dB more analog headroom. The floor of 1
# lets a power-user hand-tune one notch down without our self-heal
# overwriting them. Line DAC at raw value 100 maps to roughly -9 dB —
# the v0.1.51 → v0.1.59 calibration; anything at or above that is
# "loud enough" and we leave it alone. The current baseline is 115
# (-1.5 dB).
_TLV320_LINE_MIN_VALUE = 1
_TLV320_LINE_DAC_MIN_VALUE = 100

_JARVIS_ALSA_STORE = "/usr/local/sbin/jarvis-alsa-store"


def _read_playback_value_and_switch(
    card: str, control: str
) -> tuple[int | None, bool | None]:
    """Return ``(raw_value, switch_on)`` for an amixer playback control.

    ``switch_on`` is True when the pswitch reads ``[on]`` (signal passes,
    unmuted), False when ``[off]`` (muted), None when the control has no
    pswitch. Returns ``(None, None)`` when the control isn't readable.
    """
    result = _run(["amixer", "-c", card, "sget", control])
    if result is None or result.returncode != 0:
        return None, None
    # The Playback line looks like:
    #   Front Left: Playback 71 [60%] [-23.50dB]                (no switch)
    #   Front Left: Playback 0 [0%] [0.00dB] [on]               (has switch)
    # Mono variant:
    #   Mono: Playback 127 [100%] [0.00dB] [on]
    line = next(
        (l for l in result.stdout.splitlines() if "Playback" in l and "%" in l),
        None,
    )
    if line is None:
        return None, None
    val_match = re.search(r"Playback (\d+)", line)
    if not val_match:
        return None, None
    # Switch token, if present, is the final ``[on]`` or ``[off]`` on the
    # line — anchor to end-of-line so the dB column can't accidentally
    # match.
    switch_match = re.search(r"\[(on|off)\]\s*$", line)
    switch_on = (switch_match.group(1) == "on") if switch_match else None
    return int(val_match.group(1)), switch_on


def ensure_output_baseline() -> None:
    """Re-assert the TLV320AIC3104 output mixer baseline if it has drifted.

    Idempotent and safe to call on every startup. No-op when:
      - codec isn't present (macOS dev node, plain Pi without the HAT)
      - Line and Line DAC are already at or above the calibrated floor
        AND HP/HPCOM are already muted (the typical happy path)
      - mixer state is unreadable (codec busy, ALSA glitch — logged,
        retried on next startup)

    When applied, persists via the jarvis-alsa-store sudo wrapper so
    alsa-restore picks up the calibrated values on the next boot.
    """
    card = get_audio_card()
    if card != "seeed2micvoicec":
        return

    line_value, _ = _read_playback_value_and_switch(card, "Line")
    line_dac_value, _ = _read_playback_value_and_switch(card, "Line DAC")
    _, hp_on = _read_playback_value_and_switch(card, "HP")
    _, hpcom_on = _read_playback_value_and_switch(card, "HPCOM")

    if (
        line_value is None
        or line_dac_value is None
        or hp_on is None
        or hpcom_on is None
    ):
        logger.info(
            "Output mixer state unreadable, skipping self-heal (codec may be busy)",
            card=card,
            line=line_value,
            line_dac=line_dac_value,
            hp_on=hp_on,
            hpcom_on=hpcom_on,
        )
        return

    drift = (
        line_value < _TLV320_LINE_MIN_VALUE
        or line_dac_value < _TLV320_LINE_DAC_MIN_VALUE
        or hp_on  # HP should be muted (switch off)
        or hpcom_on  # HPCOM should be muted (switch off)
    )
    if not drift:
        return

    logger.warning(
        "Output mixer below calibrated baseline — re-asserting",
        card=card,
        line=line_value,
        line_dac=line_dac_value,
        hp_unmuted=hp_on,
        hpcom_unmuted=hpcom_on,
        target_line=4,
        target_line_dac=115,
    )

    for control_args in _TLV320_OUTPUT_BASELINE_CMDS:
        cmd = ["amixer", "-c", card, "sset", *control_args]
        result = _run(cmd)
        if result is None or result.returncode != 0:
            logger.error(
                "Output baseline amixer command failed",
                control=control_args[0],
                stderr=(result.stderr if result else None),
            )

    # Persist so alsa-restore picks it up next boot — otherwise the heal
    # re-fires forever (and the next boot's first playback before
    # jarvis-node starts is still quiet).
    persist = _run(["sudo", "-n", _JARVIS_ALSA_STORE])
    if persist is None or persist.returncode != 0:
        logger.warning(
            "jarvis-alsa-store failed — output baseline won't survive reboot",
            stderr=(persist.stderr if persist else None),
        )


# ── Persistence (config.json) ───────────────────────────────────────────────


def _config_path() -> str:
    return os.path.expandvars(os.path.expanduser(
        os.environ.get("CONFIG_PATH", "config.json")
    ))


def _read_persisted_user_pct() -> int | None:
    """Return ``volume_percent`` from config.json, or None."""
    try:
        with open(_config_path()) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    val = config.get("volume_percent")
    if val is None:
        return None
    try:
        return max(0, min(100, int(val)))
    except (TypeError, ValueError):
        return None


def persist_volume_percent(pct: int) -> bool:
    """Write ``volume_percent`` to config.json so the value survives reboot.

    Called automatically from ``set_volume_percent`` after a successful
    apply — callers don't usually invoke this directly.
    """
    pct = max(0, min(100, int(pct)))
    try:
        # Serialized + atomic with every other config.json writer — an
        # unlocked read-modify-write here could silently restore a value
        # (incl. a policy consent key) another writer just changed.
        update_config_file(lambda config: config.__setitem__("volume_percent", pct))
        return True
    except OSError as e:
        logger.warning("persist volume_percent failed", error=str(e))
        return False


# ── PulseAudio helpers ──────────────────────────────────────────────────────


def _list_bluez_sinks() -> list[str]:
    """Return PulseAudio sink names matching ``bluez_sink.*``. Empty if none."""
    result = _run(["pactl", "list", "short", "sinks"])
    if result is None or result.returncode != 0:
        return []
    sinks: list[str] = []
    for line in result.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1].startswith("bluez_sink."):
            sinks.append(cols[1])
    return sinks


def _list_all_output_sinks() -> list[str]:
    """Return every non-monitor PulseAudio sink. Empty if none / pactl missing.

    On a fresh-install Pi (issue surfaced in beta May 2026), the audio
    driver enumerates `vc4-hdmi` first and PA picks that as
    ``@DEFAULT_SINK@`` even when the user's speaker is the ReSpeaker
    HAT — so setting volume on `@DEFAULT_SINK@` succeeds but the user
    hears no change. Apply to every output sink as a fallback.
    """
    result = _run(["pactl", "list", "short", "sinks"])
    if result is None or result.returncode != 0:
        return []
    sinks: list[str] = []
    for line in result.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and not cols[1].endswith(".monitor"):
            sinks.append(cols[1])
    return sinks


def _pactl_set_sink_volume(sink: str, pct: int) -> bool:
    result = _run(["pactl", "set-sink-volume", sink, f"{pct}%"])
    return result is not None and result.returncode == 0


def _pactl_set_sink_mute(sink: str, muted: bool) -> bool:
    result = _run(["pactl", "set-sink-mute", sink, "1" if muted else "0"])
    return result is not None and result.returncode == 0


def _pactl_get_default_sink_volume() -> int | None:
    """Parse the highest channel % from ``pactl get-sink-volume @DEFAULT_SINK@``."""
    result = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if result is None or result.returncode != 0:
        return None
    # Example output: "Volume: front-left: 32768 /  50% / -18.00 dB,
    #                          front-right: 32768 /  50% / -18.00 dB"
    matches = re.findall(r"(\d+)%", result.stdout)
    if not matches:
        return None
    return max(int(m) for m in matches)


def _pactl_get_default_sink_mute() -> bool | None:
    """Return True/False from ``pactl get-sink-mute @DEFAULT_SINK@``, or None."""
    result = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    if result is None or result.returncode != 0:
        return None
    # Example output: "Mute: yes" or "Mute: no"
    line = result.stdout.strip().lower()
    if line.endswith("yes"):
        return True
    if line.endswith("no"):
        return False
    return None


# ── Public API ──────────────────────────────────────────────────────────────


def get_volume_percent() -> int | None:
    """Return current volume as 0-100, or None if unreadable.

    Prefers the persisted value in config.json so the contract is exact —
    ``set_volume_percent(70)`` → ``get_volume_percent()`` returns 70 (no
    drift through PulseAudio's integer percent). Falls back to the live
    PA reading when config.json has no volume_percent yet, so the mobile
    slider still has something to display on first boot.
    """
    persisted = _read_persisted_user_pct()
    if persisted is not None:
        return persisted
    return _pactl_get_default_sink_volume()


def is_muted() -> bool | None:
    """Return True if PA's default sink is muted, False if not, None if unreadable."""
    return _pactl_get_default_sink_mute()


def set_volume_percent(pct: int) -> bool:
    """Clamp to [0, 100] and apply across every output sink we can see.

    Persists to config.json first so the requested level survives reboot
    even when PulseAudio hasn't enumerated the audio card yet (the
    fresh-install case — PA daemon isn't up on first boot until the
    first sink-using process triggers it). After persisting, applies
    live to @DEFAULT_SINK@; if that fails, falls back to enumerating
    every non-monitor sink and applying to each (covers the case where
    HDMI ended up as the default sink but the user's speaker is on
    another sink).

    Returns True if any sink was successfully updated; persistence
    happens unconditionally so the next boot picks up the right value.
    """
    pct = max(0, min(100, int(pct)))

    # Persist first — even if PA is still warming up, the value sticks
    # for next boot. The MQTT handler also writes config.json after
    # this call, but doing it here makes set_volume_percent self-
    # contained for voice-driven adjustments ("volume up").
    persist_volume_percent(pct)

    default_ok = _pactl_set_sink_volume("@DEFAULT_SINK@", pct)

    fallback_sinks: list[str] = []
    if not default_ok:
        for sink in _list_all_output_sinks():
            if _pactl_set_sink_volume(sink, pct):
                fallback_sinks.append(sink)

    for sink in _list_bluez_sinks():
        _pactl_set_sink_volume(sink, pct)

    if default_ok or fallback_sinks:
        logger.info(
            "Volume set",
            percent=pct,
            default_sink_ok=default_ok,
            fallback_sinks=fallback_sinks,
        )
        return True

    logger.warning(
        "pactl set-sink-volume failed for every sink — persisted only",
        percent=pct,
    )
    return False


def adjust_volume_percent(delta: int) -> int | None:
    """Add ``delta`` percentage points, clamped to [0, 100]. None if unreadable."""
    current = get_volume_percent()
    if current is None:
        return None
    new = max(0, min(100, current + int(delta)))
    if not set_volume_percent(new):
        return None
    return new


def set_muted(muted: bool) -> bool:
    """Mute/unmute every output sink we can see.

    Same fresh-install resilience as ``set_volume_percent``: if
    @DEFAULT_SINK@ fails or points at the wrong sink (e.g. HDMI on a
    first-boot Pi), fall back to enumerating every non-monitor sink.
    Returns True if any sink was updated.
    """
    default_ok = _pactl_set_sink_mute("@DEFAULT_SINK@", muted)

    fallback_sinks: list[str] = []
    if not default_ok:
        for sink in _list_all_output_sinks():
            if _pactl_set_sink_mute(sink, muted):
                fallback_sinks.append(sink)

    for sink in _list_bluez_sinks():
        _pactl_set_sink_mute(sink, muted)

    if default_ok or fallback_sinks:
        logger.info(
            "Mute set",
            muted=muted,
            default_sink_ok=default_ok,
            fallback_sinks=fallback_sinks,
        )
        return True

    logger.warning("pactl set-sink-mute failed for every sink", muted=muted)
    return False
