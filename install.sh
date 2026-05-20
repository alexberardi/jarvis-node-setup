#!/bin/bash
# Jarvis Node Installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/alexberardi/jarvis-node-setup/main/install.sh | sudo bash
#
# Flags:
#   --no-audio     Skip ALSA / I2S DAC configuration
#   --force        Reinstall even if already at latest version
#   --version TAG  Install a specific version (e.g. v0.1.0)
#   --local        Skip download (tarball already extracted to /opt/jarvis-node)

set -euo pipefail

# --- Tracing ---
# Write every executed command to stderr with file:line:function context.
# bash's stderr goes straight through to the log file via `2>&1`; it's not
# stdio-buffered the way stdout+ would be, so the LAST trace line in the
# log IS the command that was running when the script died. Ends the
# "silent death between functions" guesswork that's masked this bug for
# several versions.
#
# Earlier v0.1.16 tried to re-exec under `stdbuf -oL -eL` for buffering
# safety, but that's unworkable when the installer runs via
# `curl | bash -s -- --force --version vX.Y.Z`: `$0` is the shell name
# ("bash"), so exec'ing `stdbuf ... "$0" "$@"` becomes
# `stdbuf bash --force ...` and bash rejects --force as an unknown flag
# before install.sh even gets to download. Dropped.
# PS4 uses $LINENO + $FUNCNAME only — NOT $BASH_SOURCE — because under
# `bash -s` (piped from curl) $BASH_SOURCE is unset, and with `set -u`
# active that triggers "unbound variable" on every single traced command.
# Previous v0.1.17 attempt failed on line 42 with exactly that.
export PS4='+ [L${LINENO}${FUNCNAME:+:${FUNCNAME[0]}}] '
set -x

# --- Exit trap ---
# Capture final status regardless of how we leave (success, set -e
# failure, signal). Prints a grep-able `[INSTALL-EXIT]` line with the
# offending command + line + function so post-mortem is obvious.
trap '_ec=$?;
      _line=$LINENO;
      _func=${FUNCNAME:-main};
      _last=$BASH_COMMAND;
      set +x;
      printf "[INSTALL-EXIT] status=%d line=%s func=%s last_command=%q\\n" \
             "$_ec" "$_line" "$_func" "$_last" >&2' EXIT

REPO="alexberardi/jarvis-node-setup"
INSTALL_DIR="/opt/jarvis-node"
SERVICE_NAME="jarvis-node"

# Service runs as a non-root user so PulseAudio (per-user) and the
# bluetooth stack work for audio routing. See
# docs/migration-to-pi-service.md.
SERVICE_USER="pi"
SERVICE_HOME="/home/pi"

# --- Defaults ---
SKIP_AUDIO=0
FORCE=0
TARGET_VERSION=""
LOCAL_MODE=0

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { printf "${BLUE}>${NC} %s\n" "$1"; }
success() { printf "${GREEN}>${NC} %s\n" "$1"; }
error()   { printf "${RED}>${NC} %s\n" "$1" >&2; exit 1; }
warn()    { printf "${RED}>${NC} %s\n" "$1" >&2; }

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-audio)    SKIP_AUDIO=1; shift ;;
    --force)       FORCE=1; shift ;;
    --version)     TARGET_VERSION="$2"; shift 2 ;;
    --local)       LOCAL_MODE=1; shift ;;
    *)             warn "Unknown flag: $1"; shift ;;
  esac
done

# --- Preflight checks ---
preflight() {
  printf "\n${BOLD}Jarvis Node Installer${NC}\n\n"

  # Must be root
  if [ "$(id -u)" -ne 0 ]; then
    error "This installer must be run as root (use sudo)"
  fi

  # Must be Linux
  if [ "$(uname -s)" != "Linux" ]; then
    error "This installer is for Linux (Raspberry Pi). Got: $(uname -s)"
  fi

  # Detect architecture
  ARCH="$(uname -m)"
  case "$ARCH" in
    armv7l|armv6l) ARCH="armv7l" ;;
    aarch64)       ARCH="arm64" ;;
    *)             error "Unsupported architecture: $ARCH (expected armv7l or arm64)" ;;
  esac
  info "Architecture: ${ARCH}"

  # Detect Pi model (informational)
  if [ -f /sys/firmware/devicetree/base/model ]; then
    PI_MODEL="$(tr -d '\0' < /sys/firmware/devicetree/base/model)"
    info "Hardware: ${PI_MODEL}"
  fi

  # Check for curl
  if ! command -v curl >/dev/null 2>&1; then
    error "curl is required but not installed. Run: apt-get install curl"
  fi

  # Ensure swap is available — Pi Zero 2W has only 512MB RAM which is
  # not enough for extracting large tarballs or pip-compiling packages.
  if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
    info "No swap detected — creating 1GB swapfile..."
    fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    # Persist across reboots
    if ! grep -q '/swapfile' /etc/fstab; then
      echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    success "Swap enabled (1GB, persistent)"
  fi
}

# --- Version check ---
get_version() {
  if [ -n "$TARGET_VERSION" ]; then
    # Strip leading v if present
    VERSION="${TARGET_VERSION#v}"
    TAG="v${VERSION}"
    info "Target version: ${TAG}"
    return
  fi

  info "Checking latest version..."
  TAG="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')"

  if [ -z "$TAG" ]; then
    error "Could not determine latest version. Check https://github.com/${REPO}/releases"
  fi

  VERSION="${TAG#v}"
  info "Latest version: ${TAG}"

  # Check if already installed at this version
  if [ "$FORCE" -eq 0 ] && [ -f "${INSTALL_DIR}/VERSION" ]; then
    INSTALLED="$(cat "${INSTALL_DIR}/VERSION")"
    if [ "$INSTALLED" = "$VERSION" ]; then
      success "Already at version ${VERSION}. Use --force to reinstall."
      exit 0
    fi
    info "Upgrading from ${INSTALLED} to ${VERSION}"
  fi
}

# --- Detect Python ---
detect_python() {
  # Raspberry Pi OS Bookworm ships Python 3.11 as "python3" without a
  # versioned "python3.11" package.  Detect which form is available so
  # apt-get doesn't fail on a fresh install.
  if apt-cache show python3.11 >/dev/null 2>&1; then
    PY_PKG="python3.11"
    PY_VENV_PKG="python3.11-venv"
    PY_BIN="python3.11"
  else
    PY_PKG="python3"
    PY_VENV_PKG="python3-venv"
    PY_BIN="python3"
  fi
  info "Python package: ${PY_PKG} ($(${PY_BIN} --version 2>&1))"
}

# --- Install system packages ---
install_apt_deps() {
  detect_python

  # Build the list of packages we'd want installed. Include audio packages
  # only when --no-audio isn't set.
  local wanted=(
    "${PY_PKG}" "${PY_VENV_PKG}"
    git avahi-utils hostapd dnsmasq
    sqlcipher libsqlcipher-dev libopenblas-dev
    mosquitto-clients
  )
  if [ "$SKIP_AUDIO" -eq 0 ]; then
    # mpv is used by streaming commands (Pandora, etc.) because its --ao=alsa
    # backend opens the asound.conf "output" PCM directly. ffplay's SDL2 ALSA
    # path fails to open the device when spawned as a subprocess of the
    # systemd-managed jarvis-node service (works fine from a login shell —
    # the difference is opaque enough that we just prefer mpv).
    wanted+=(alsa-utils portaudio19-dev sox ffmpeg mpv espeak
             pulseaudio pulseaudio-module-bluetooth
             device-tree-compiler
             python3-lgpio)
  fi

  # Filter to only packages that aren't already installed. On upgrades this
  # is almost always empty — dpkg inspection is cheap (< 1s for 14 packages)
  # whereas `apt-get install` of already-present packages takes 10-20 minutes
  # on Pi Zero due to its slow SD card and per-package state verification.
  local missing=()
  local pkg
  for pkg in "${wanted[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
      missing+=("$pkg")
    fi
  done

  # Always (re)assert hostapd/dnsmasq masking — the node manages these
  # directly and auto-start from systemd would conflict with provisioning.
  systemctl stop hostapd dnsmasq 2>/dev/null || true
  systemctl disable hostapd dnsmasq 2>/dev/null || true
  systemctl mask hostapd dnsmasq 2>/dev/null || true

  # Disable graphical desktop on Pi nodes. Pi OS Desktop's Wayland stack
  # (labwc + wf-panel-pi + pcmanfm) wastes ~75-100 MB RAM on a node that
  # has no display attached, and on a 416 MB Pi Zero 2W that pushes the
  # system into SD-card swap during normal jarvis-node operation — which
  # under combined audio + Wi-Fi + BT load causes hard kernel-level wedges
  # (solid green LED, no SD activity, no journal entries).
  if [ "$(systemctl get-default 2>/dev/null)" = "graphical.target" ]; then
    info "Disabling graphical desktop (frees ~75-100 MB RAM on next boot)"
    systemctl set-default multi-user.target >/dev/null
  fi

  if [ ${#missing[@]} -eq 0 ]; then
    info "System dependencies already installed — skipping apt"
    return
  fi

  info "Installing ${#missing[@]} missing package(s): ${missing[*]}"

  # Refresh apt cache only when missing packages means we actually care.
  local apt_cache="/var/cache/apt/pkgcache.bin"
  if [ ! -f "$apt_cache" ] || [ "$(( $(date +%s) - $(stat -c %Y "$apt_cache" 2>/dev/null || echo 0) ))" -gt 3600 ]; then
    apt-get update -qq
  fi

  apt-get install -y --no-install-recommends -qq "${missing[@]}" > /dev/null

  success "System dependencies installed"
}

# --- Download and extract tarball ---
download_and_extract() {
  if [ "$LOCAL_MODE" -eq 1 ]; then
    info "Local mode: skipping download (using existing ${INSTALL_DIR})"
    return
  fi

  local tarball="jarvis-node-${VERSION}-${ARCH}.tar.gz"
  local url="https://github.com/${REPO}/releases/download/${TAG}/${tarball}"

  # Back up existing install
  if [ -d "$INSTALL_DIR" ]; then
    local backup="${INSTALL_DIR}.bak"
    info "Backing up existing install to ${backup}"
    rm -rf "$backup"
    mv "$INSTALL_DIR" "$backup"
  fi

  # Stream directly to tar — avoids buffering the full tarball in /tmp
  # (which is tmpfs / RAM-backed) and prevents OOM on 512MB boards.
  info "Downloading and extracting ${tarball}..."
  if ! curl -fSL "$url" | tar xzf - -C /; then
    error "Download/extract failed. Check: https://github.com/${REPO}/releases/tag/${TAG}"
  fi

  success "Extracted to ${INSTALL_DIR}"

  # Restore user data from the .bak dir. Without this, an upgrade blows
  # away config.json/DB and the node drops back to provisioning mode.
  local backup="${INSTALL_DIR}.bak"
  if [ -d "$backup" ]; then
    for f in config.json .env; do
      if [ -f "${backup}/${f}" ]; then
        cp "${backup}/${f}" "${INSTALL_DIR}/${f}"
        info "Restored ${f} from backup"
      fi
    done
    # Encrypted SQLite DB + WAL/SHM journals.
    #
    # Use `if [ -f ... ]` rather than the `[ ... ] && cmd && cmd` idiom:
    # when a glob pattern matches nothing, bash leaves the literal
    # pattern as the loop variable (e.g. "/opt/jarvis-node.bak/*.db-wal").
    # The test then returns 1 for "file doesn't exist", and under
    # `set -e` that non-zero becomes the loop's final exit status, which
    # becomes download_and_extract's return value, which kills the entire
    # script from main's context — silently, because the failure is many
    # frames away from where we "expected" exit to happen. The explicit
    # `if` is set-e-safe because the test is the if-condition (exempt).
    for f in "${backup}"/*.db "${backup}"/*.db-shm "${backup}"/*.db-wal; do
      if [ -f "$f" ]; then
        cp "$f" "${INSTALL_DIR}/"
        info "Restored $(basename "$f") from backup"
      fi
    done

    # Pantry-installed custom components (commands, agents, device protocols,
    # device managers, routines). These live inside the install dir and would
    # otherwise be lost when the tarball overwrites it.
    local custom_dirs=(
      "commands/custom_commands"
      "agents/custom_agents"
      "device_families/custom_families"
      "device_managers/custom_managers"
      "routines/custom_routines"
    )
    for rel in "${custom_dirs[@]}"; do
      if [ -d "${backup}/${rel}" ]; then
        mkdir -p "${INSTALL_DIR}/${rel}"
        # Copy each sub-directory (one per installed package component)
        for pkg_dir in "${backup}/${rel}"/*/; do
          if [ -d "$pkg_dir" ]; then
            local pkg_name
            pkg_name="$(basename "$pkg_dir")"
            cp -a "$pkg_dir" "${INSTALL_DIR}/${rel}/${pkg_name}"
            info "Restored custom ${rel}/${pkg_name} from backup"
          fi
        done
      fi
    done
  fi
}

# --- Configure audio ---
# Target hardware: Seeed ReSpeaker 2-mics Pi HAT v2.0.
# - TLV320AIC3104 codec on I2C bus 1 @ 0x18, I2S audio link
#   (NOT WM8960 — Seeed swapped codecs between v1 and v2; the v2 needs
#   Seeed's `respeaker-2mic-v2_0-overlay` which we vendor in `setup/`)
# - JST speaker output via the codec's HP path
# - 3x APA102 RGB LEDs on SPI (see services/respeaker_led_service.py)
# - GPIO17 user button (see services/button_service.py)
configure_audio() {
  if [ "$SKIP_AUDIO" -eq 1 ]; then
    info "Skipping audio configuration (--no-audio)"
    return
  fi

  info "Configuring audio for ReSpeaker 2-mics Pi HAT v2.0..."

  # --- Install the Seeed-published v2.0 DT overlay ---
  # Pi OS doesn't ship an overlay for the v2.0 hardware. We vendor Seeed's
  # DTS at setup/respeaker-2mic-v2_0-overlay.dts and compile it on the Pi
  # against the running kernel (dtc was installed by install_apt_deps).
  local overlay_src="${INSTALL_DIR}/setup/respeaker-2mic-v2_0-overlay.dts"
  local overlay_dest="/boot/firmware/overlays/respeaker-2mic-v2_0-overlay.dtbo"
  if [ ! -d /boot/firmware/overlays ] && [ -d /boot/overlays ]; then
    overlay_dest="/boot/overlays/respeaker-2mic-v2_0-overlay.dtbo"
  fi
  if [ -f "$overlay_src" ]; then
    info "Compiling respeaker-2mic-v2_0 overlay from $(basename "$overlay_src")"
    local overlay_tmp
    overlay_tmp="$(mktemp --suffix=.dtbo)"
    if dtc -W no-unit_address_vs_reg -I dts "$overlay_src" -o "$overlay_tmp" >/dev/null 2>&1; then
      cp "$overlay_tmp" "$overlay_dest"
      success "Overlay installed: $overlay_dest"
    else
      warn "dtc failed to compile overlay — audio will not work until fixed"
    fi
    rm -f "$overlay_tmp"
  else
    warn "Overlay source missing at $overlay_src — skipping"
  fi

  # --- /boot/firmware/config.txt — kernel overlay + bus enables ---
  local config_file="/boot/firmware/config.txt"
  if [ ! -f "$config_file" ]; then
    config_file="/boot/config.txt"
  fi

  if [ -f "$config_file" ]; then
    # Disable BCM2835 audio — we use the HAT's TLV320AIC3104 via I2S
    sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$config_file"

    # Migration: comment out previous overlays from past installs (HifiBerry
    # in the USB-mic era; mainline wm8960-soundcard from when we thought
    # the HAT was v1.0). Audit trail stays in the file as commented lines.
    if grep -q "^dtoverlay=hifiberry-dac" "$config_file"; then
      sed -i 's/^dtoverlay=hifiberry-dac/#dtoverlay=hifiberry-dac # replaced for ReSpeaker v2.0/' "$config_file"
      info "Disabled previous HifiBerry overlay"
      NEEDS_REBOOT=1
    fi
    if grep -q "^dtoverlay=wm8960-soundcard" "$config_file"; then
      sed -i 's/^dtoverlay=wm8960-soundcard/#dtoverlay=wm8960-soundcard # replaced for ReSpeaker v2.0/' "$config_file"
      info "Disabled previous wm8960-soundcard overlay"
      NEEDS_REBOOT=1
    fi

    # Seeed ReSpeaker 2-mic HAT v2.0 — TLV320AIC3104
    if ! grep -q "^dtoverlay=respeaker-2mic-v2_0-overlay" "$config_file"; then
      echo "dtoverlay=respeaker-2mic-v2_0-overlay" >> "$config_file"
      info "ReSpeaker v2.0 overlay added"
      NEEDS_REBOOT=1
    fi

    # SPI for the HAT's 3x APA102 RGB LEDs
    if ! grep -q "^dtparam=spi=on" "$config_file"; then
      echo "dtparam=spi=on" >> "$config_file"
      info "SPI enabled for APA102 LEDs"
      NEEDS_REBOOT=1
    fi

    # I2C for the HAT's Grove ports (future-proofing — sensors, etc.)
    if ! grep -q "^dtparam=i2c_arm=on" "$config_file"; then
      echo "dtparam=i2c_arm=on" >> "$config_file"
      info "I2C enabled"
      NEEDS_REBOOT=1
    fi

    success "Boot config updated"
  else
    warn "Could not find config.txt — skipping I2S DAC setup"
  fi

  # --- ALSA module ordering ---
  # TLV320AIC3104 is the only audio card we care about. CARD= name matching
  # in asound.conf is the authoritative pin; this is belt-and-suspenders.
  # (The `index=N` option on the codec module is ignored by the kernel and
  # logs "unknown parameter" — but harmless.)
  cat > /etc/modprobe.d/alsa-base.conf <<'ALSA_MOD'
options snd-soc-tlv320aic3x index=0
ALSA_MOD

  # --- ALSA system config ---
  # Hybrid PA/ALSA: PA for playback, direct dsnoop for capture.
  #
  # PLAYBACK → PulseAudio. PA's sink-input mixing lets the wake response
  # and any media player (mpv/ffplay/spotifyd/...) emit audio concurrently.
  # The TLV320AIC3104 codec exposes a single playback substream, so
  # softvol-direct gave the second opener ENOSPC ("Device or resource
  # busy"), and dmix on this codec fails ("unable to initialize sum ring
  # buffer"). PA is the working solution for concurrent playback.
  #
  # CAPTURE → direct hardware via dsnoop. The previous config (which
  # routed dsnoopmic through PA too) added 25-50ms of buffering AND
  # exposed the wake-word audio stream to PA's input-side resampling
  # and any noise-suppression / AGC modules. Together they degraded
  # openWakeWord scores enough that "hey jarvis" became unreliable on
  # the ReSpeaker HAT. dsnoop is built for sharing a single capture
  # device among multiple openers (the wake-word loop + the barge-in
  # monitor) at zero added latency. The plug wrapper handles
  # channel/format adaptation for callers that want mono.
  #
  # Alias names `output` and `dsnoopmic` are referenced from app code
  # (config.example.json's `mic_device_name="dsnoopmic"` and
  # platform_abstraction.py's `aplay -D output`). They stay as the
  # public interface — only the internals change.
  cat > /etc/asound.conf <<'ASOUND'
# --- Playback -----------------------------------------------------------
pcm.output {
    type pulse
}

# --- Capture ------------------------------------------------------------
pcm.dsnoopmic_hw {
    type dsnoop
    ipc_key 87654321
    slave {
        pcm "hw:CARD=seeed2micvoicec,DEV=0"
        channels 2
        rate 48000
        format S16_LE
        # Cap dsnoop's ring buffer at ~85ms so the wake-word loop can never
        # be scoring stale audio. Without explicit sizes, dsnoop chooses
        # defaults that can hold hundreds of ms; under CPU bursts (LLM
        # responses, TTS streaming, music mixing) the main thread falls
        # behind on consuming chunks and the model ends up evaluating
        # audio from N hundred ms ago — user-perceived "wake took
        # forever to fire". 1024-sample periods × 4 = 4096-sample buffer
        # at 48kHz = 21ms × 4 = 85ms. Under-runs (xruns) on a too-slow
        # reader are preferable to stale audio: the model misses some
        # frames but is always scoring "now".
        period_size 1024
        buffer_size 4096
    }
}

pcm.dsnoopmic {
    type plug
    slave.pcm "dsnoopmic_hw"
}

# --- Default split: playback via PA, capture direct --------------------
pcm.!default {
    type asym
    playback.pcm "output"
    capture.pcm "dsnoopmic"
}

ctl.!default {
    type pulse
}
ASOUND

  success "ALSA configuration written"

  # --- TLV320AIC3104 mixer baseline ---
  # The codec defaults leave HP/speaker output muted (HP gain = 0) and PCM
  # at -23.5 dB — playback technically works but is inaudible. Set sensible
  # levels once at install time and store with alsactl so they persist
  # across reboots. After this, runtime volume is controlled entirely by
  # PulseAudio (pactl) — see utils/audio_volume.py.
  #
  # On the ReSpeaker v2.0 the JST speaker is wired to the codec's
  # LLOUT/RLOUT (Line Out) path — NOT HPLOUT/HPROUT. Driving the HP path
  # can't power the speaker effectively (HP outs are ~25 mW into 16 ohm)
  # AND driving Line at full amplification was dangerously loud in
  # calibration ("had to yank Pi power" loud). Safe baseline:
  #   - PCM (DAC digital) at full scale — gives PA headroom
  #   - Line (analog gain) at +4 dB (control value 4/9). 0 dB on its
  #     own was inaudibly quiet on the JST speaker even at full digital
  #     gain; +4 dB combined with the Line DAC level below gives a usable
  #     "party-level" max at PA 100%. Stops well short of the +9 dB max
  #     that calibration found dangerously loud.
  #   - Line DAC (digital mixer level) at -1.5 dB (control value 115/118).
  #     The earlier baseline of 78 (-20 dB) was way too quiet; bumping to
  #     -1.5 dB plus the analog +4 dB matches the level hand-tuned on
  #     prod kitchen and confirmed comfortable at PA 100%.
  #   - HP / HPCOM paths kept muted (unused, conserves power and prevents
  #     the slight crosstalk we observed during testing)
  # Each amixer call is `|| true` so an unknown control name on a future
  # kernel doesn't abort the installer.
  if command -v amixer >/dev/null 2>&1 && aplay -l 2>/dev/null | grep -qi seeed2micvoicec; then
    info "Applying TLV320AIC3104 mixer baseline..."
    amixer -c seeed2micvoicec sset 'PCM' '100%' unmute               2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Line' '4' unmute                 2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Line DAC' '115' unmute           2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Left Line Mixer DACL1' on        2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Right Line Mixer DACR1' on       2>/dev/null || true
    amixer -c seeed2micvoicec sset 'HP' '0' mute                     2>/dev/null || true
    amixer -c seeed2micvoicec sset 'HP DAC' '0' mute                 2>/dev/null || true
    amixer -c seeed2micvoicec sset 'HPCOM' '0' mute                  2>/dev/null || true
    amixer -c seeed2micvoicec sset 'HPCOM DAC' '0' mute              2>/dev/null || true
    amixer -c seeed2micvoicec sset 'PGA' '60%'                       2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Left PGA Mixer Line1L' on        2>/dev/null || true
    amixer -c seeed2micvoicec sset 'Right PGA Mixer Line1R' on       2>/dev/null || true
    alsactl store 2>/dev/null || true
    success "TLV320AIC3104 mixer baseline applied"
  else
    info "seeed2micvoicec not detected yet (overlay loads on reboot) — mixer baseline applies on next install run"
  fi

  # --- Group membership for SPI / GPIO / I2C ---
  # The jarvis-node service runs as ${SERVICE_USER}. /dev/spidev* (APA102
  # LEDs), /dev/gpiomem (GPIO17 button), and /dev/i2c-* are owned by the
  # `spi`, `gpio`, and `i2c` groups respectively. Without membership the
  # service runs but fails to open these devices with EACCES.
  for grp in spi gpio i2c; do
    if getent group "$grp" >/dev/null 2>&1; then
      usermod -aG "$grp" "$SERVICE_USER" 2>/dev/null || true
    fi
  done

  # --- Sanity check ---
  # On a fresh install this runs before the reboot that activates the
  # dtoverlay, so the card legitimately won't be present yet. On a re-run
  # after reboot, missing-here is a real problem.
  if aplay -l 2>/dev/null | grep -qi seeed2micvoicec; then
    success "seeed2micvoicec audio card detected"
  else
    info "Card not present yet (will appear after the reboot below)"
  fi

  # --- Disable WiFi power management ---
  # Pi Zero 2 W's wlan0 enters power-save mode after idle, dropping off the
  # network entirely. SSH becomes unreachable, MQTT disconnects, and the node
  # looks dead even though the CPU is still running. This is a one-liner in
  # NetworkManager or a cron @reboot fallback for systems using wpa_supplicant.
  if command -v nmcli >/dev/null 2>&1; then
    local wifi_con
    wifi_con="$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | grep ':802-11-wireless' | head -1 | cut -d: -f1 || true)"
    if [ -n "$wifi_con" ]; then
      nmcli connection modify "$wifi_con" wifi.powersave 2 2>/dev/null && \
        success "WiFi power-save disabled (NetworkManager)" || true
    fi
  fi
  # Persistent fallback: iwconfig at every boot via cron
  if command -v iwconfig >/dev/null 2>&1; then
    local cron_line="@reboot /sbin/iwconfig wlan0 power off 2>/dev/null || true"
    (crontab -l 2>/dev/null | grep -qF "iwconfig wlan0 power off") || \
      (crontab -l 2>/dev/null; echo "$cron_line") | crontab - && \
      success "WiFi power-off cron installed"
  fi
}

# --- Set up config and env files ---
setup_config() {
  # config.json
  if [ ! -f "${INSTALL_DIR}/config.json" ]; then
    cp "${INSTALL_DIR}/config.example.json" "${INSTALL_DIR}/config.json"
    info "Created config.json (will be filled during provisioning)"
  else
    info "config.json already exists, preserving"
  fi

  # .env
  if [ ! -f "${INSTALL_DIR}/.env" ]; then
    if [ -f "${INSTALL_DIR}/.env.example" ]; then
      cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
      info "Created .env"
    else
      touch "${INSTALL_DIR}/.env"
      info "Created empty .env (no .env.example in release)"
    fi
  else
    info ".env already exists, preserving"
  fi
}

# --- Fix or rebuild venv if bundled Python doesn't match system ---
rebuild_venv() {
  local venv_python="${INSTALL_DIR}/.venv/bin/python"
  local pyvenv_cfg="${INSTALL_DIR}/.venv/pyvenv.cfg"

  # If the bundled venv works, nothing to do
  if [ -x "$venv_python" ] && "$venv_python" --version >/dev/null 2>&1; then
    info "Bundled venv OK ($(${venv_python} --version 2>&1))"
    return
  fi

  # Fast path: if the Python major.minor matches, just fix the symlinks.
  # The CI builds the venv with /usr/local/bin/python3 (Docker) but the
  # Pi has /usr/bin/python3 — the compiled packages are still compatible.
  if [ -f "$pyvenv_cfg" ]; then
    local bundled_ver
    bundled_ver="$(grep '^version' "$pyvenv_cfg" | sed 's/.*= *//' | cut -d. -f1-2)"
    local system_ver
    system_ver="$("${PY_BIN}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"

    if [ "$bundled_ver" = "$system_ver" ]; then
      info "Python ${system_ver} matches — fixing venv symlinks..."
      local system_python_home
      system_python_home="$(dirname "$(readlink -f "$(which "${PY_BIN}")")")"

      # Update pyvenv.cfg home path
      sed -i "s|^home = .*|home = ${system_python_home}|" "$pyvenv_cfg"

      # Repoint the python symlinks
      rm -f "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/.venv/bin/python3"
      ln -s "${system_python_home}/python3" "${INSTALL_DIR}/.venv/bin/python"
      ln -s "${system_python_home}/python3" "${INSTALL_DIR}/.venv/bin/python3"

      if "${INSTALL_DIR}/.venv/bin/python" --version >/dev/null 2>&1; then
        success "Venv fixed ($(${INSTALL_DIR}/.venv/bin/python --version 2>&1))"
        return
      fi
      warn "Symlink fix failed — falling back to full rebuild"
    fi
  fi

  # Slow path: full rebuild from requirements
  info "Bundled venv incompatible with system Python — rebuilding..."

  # Use disk-backed tmpdir so pip doesn't fill the tmpfs
  export TMPDIR="${INSTALL_DIR}/tmp"
  mkdir -p "$TMPDIR"

  rm -rf "${INSTALL_DIR}/.venv"
  # --system-site-packages so the venv can read Pi OS's apt-installed
  # python3-lgpio (gpiozero's pin backend; not pip-installable on plain
  # Debian — see build/build-tarball.sh for the matching flag).
  "${PY_BIN}" -m venv --system-site-packages "${INSTALL_DIR}/.venv"
  "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip --quiet

  # Pick the right requirements file
  local req_file="${INSTALL_DIR}/requirements-pi.txt"
  if [ ! -f "$req_file" ]; then
    req_file="${INSTALL_DIR}/requirements-base.txt"
  fi
  if [ ! -f "$req_file" ]; then
    warn "No requirements file found — venv will have no packages"
    return
  fi

  # Strip onnxruntime from requirements — on armv7l there is no cp313 wheel
  # so it must be attempted separately to avoid blocking the whole install.
  local tmp_req="${TMPDIR}/jarvis-requirements.txt"
  grep -v "^onnxruntime" "$req_file" > "$tmp_req" || true

  local pip_args=""
  if [ "$ARCH" = "armv7l" ]; then
    pip_args="--extra-index-url https://www.piwheels.org/simple"
  fi

  info "Installing Python packages (this may take a few minutes on Pi Zero)..."
  if ! "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    -r "$tmp_req" \
    $pip_args \
    --quiet 2>&1; then
    warn "Some packages failed to install — node may have reduced functionality"
  fi

  # onnxruntime: arm64 has PyPI wheels, armv7l may not
  info "Installing onnxruntime (best-effort)..."
  "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    "onnxruntime>=1.16.0,!=1.25.1" \
    $pip_args \
    --quiet 2>/dev/null \
    || warn "onnxruntime unavailable — wake word detection will be disabled"

  # openwakeword (installed without deps to avoid pulling ai-edge-litert)
  "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    openwakeword --no-deps \
    $pip_args \
    --quiet 2>/dev/null || warn "openwakeword install failed (non-fatal)"

  # jarvis-command-sdk (bundled in tarball)
  if [ -d "${INSTALL_DIR}/jarvis-command-sdk" ]; then
    "${INSTALL_DIR}/.venv/bin/python" -m pip install \
      "${INSTALL_DIR}/jarvis-command-sdk" \
      --quiet 2>/dev/null || warn "jarvis-command-sdk install failed (non-fatal)"
  fi

  rm -rf "$TMPDIR"
  unset TMPDIR
  success "Venv rebuilt with system Python ($(${INSTALL_DIR}/.venv/bin/python --version 2>&1))"
}

# --- Run database migrations ---
setup_database() {
  info "Running database migrations..."

  cd "$INSTALL_DIR"
  # Point at the service user's secret dir so db.key (created on
  # first run) lands in ${SERVICE_HOME}/.jarvis, matching where the
  # running service will look for it. Without this, root's
  # Path.home()/.jarvis (= /root/.jarvis) would get the key and
  # the service would create a fresh, mismatched key on first boot.
  local secret_dir="${SERVICE_HOME}/.jarvis"
  if ! JARVIS_SECRET_DIRECTORY="$secret_dir" \
       "${INSTALL_DIR}/.venv/bin/python" -m alembic upgrade head 2>/dev/null; then
    # If migration fails (encrypted DB with lost key), back up and retry
    local db_file="${INSTALL_DIR}/jarvis_node.db"
    if [ -f "$db_file" ]; then
      local backup="${db_file}.bak.$(date +%Y%m%d%H%M%S)"
      warn "Migration failed — backing up database to $(basename "$backup")"
      cp "$db_file" "$backup"
      rm -f "$db_file"
      JARVIS_SECRET_DIRECTORY="$secret_dir" \
        "${INSTALL_DIR}/.venv/bin/python" -m alembic upgrade head
    fi
  fi

  success "Database ready"
}

# --- Register built-in commands ---
register_commands() {
  info "Registering built-in commands..."
  cd "$INSTALL_DIR"
  if "${INSTALL_DIR}/.venv/bin/python" -m scripts.install_command --all --skip-deps 2>/dev/null; then
    success "Commands registered"
  else
    warn "Command registration failed (non-fatal, will retry on first boot)"
  fi
}

# --- Service-user setup ---
# Adds the service user to the bluetooth group, enables systemd
# user lingering, and pre-creates ~/.jarvis with the right ownership
# so subsequent install steps (alembic, secret writes) land in the
# service user's home rather than /root/.jarvis.
#
# Lingering creates /run/user/<uid> at boot, before the user has
# logged in — without it, jarvis-node starts before its
# XDG_RUNTIME_DIR exists and PulseAudio routing fails.
setup_service_user() {
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    error "Service user '$SERVICE_USER' does not exist. Pi Imager creates it during imaging — set up a user named '$SERVICE_USER' and re-run the installer."
  fi

  if getent group bluetooth >/dev/null 2>&1; then
    usermod -aG bluetooth "$SERVICE_USER"
  fi

  loginctl enable-linger "$SERVICE_USER" 2>/dev/null || true

  # Pre-create the secret dir so subsequent steps (alembic creating
  # db.key, etc.) land here instead of /root/.jarvis. Idempotent.
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "${SERVICE_HOME}/.jarvis"
}

# --- State migration (root → service user) ---
# Pre-migration installs kept node state in /root/.jarvis. Copy it to
# the service user's home so the service can read its keys after the
# user switch. Always re-asserts ownership of ${SERVICE_HOME}/.jarvis
# so any files created by previous steps as root (alembic's db.key
# in particular) flip to the service user.
#
# /root/.jarvis is left in place as a manual rollback safety net.
migrate_to_pi_home() {
  if [ -f /root/.jarvis/.provisioned ] && [ ! -f "${SERVICE_HOME}/.jarvis/.provisioned" ]; then
    info "Migrating /root/.jarvis → ${SERVICE_HOME}/.jarvis"
    mkdir -p "${SERVICE_HOME}/.jarvis"
    cp -a /root/.jarvis/. "${SERVICE_HOME}/.jarvis/"
    success "State migrated (left /root/.jarvis as manual backup)"
  fi
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${SERVICE_HOME}/.jarvis"
}

# --- Install dir ownership ---
# Flips /opt/jarvis-node ownership to the service user. Runs at the
# end of the install pipeline so the heavy steps (download/extract,
# pip install, alembic migrations) still run as root.
chown_install_dir() {
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
}

# --- Sudoers entry ---
# Grants the service user NOPASSWD access to the privileged binaries
# jarvis-node needs at runtime: reboot/shutdown (factory reset),
# `systemctl restart jarvis-node` (config-update self-restart), and
# the AP-mode toolkit (hostapd, dnsmasq, ip, pkill, systemctl
# stop|start of NetworkManager/wpa_supplicant/dnsmasq). Validated
# with visudo before installing — a malformed sudoers file would
# lock everyone out of sudo.
install_apt_wrapper() {
  # Copy scripts/jarvis-apt-install → /usr/local/sbin/, root-owned 0755.
  # The service user invokes this via `sudo` (see install_sudoers below) to
  # install Pantry-declared apt packages without holding broad apt privileges.
  local src="${INSTALL_DIR}/scripts/jarvis-apt-install"
  local dest="/usr/local/sbin/jarvis-apt-install"
  if [ ! -f "$src" ]; then
    warn "scripts/jarvis-apt-install missing — apt installs from Pantry will fail"
    return
  fi
  install -o root -g root -m 0755 "$src" "$dest"
  success "Installed ${dest}"
}

install_post_install_wrapper() {
  # Copy scripts/jarvis-post-install → /usr/local/sbin/, root-owned 0755.
  # The service user invokes this via `sudo` to run declarative post-install
  # ops (systemd drop-ins, config-file upserts) without holding broad system
  # privileges. Op-and-target combinations are gated server-side by Pantry's
  # post-install-allowlist.yaml.
  local src="${INSTALL_DIR}/scripts/jarvis-post-install"
  local dest="/usr/local/sbin/jarvis-post-install"
  if [ ! -f "$src" ]; then
    warn "scripts/jarvis-post-install missing — post_install ops from Pantry will fail"
    return
  fi
  install -o root -g root -m 0755 "$src" "$dest"
  success "Installed ${dest}"
}

install_self_update_wrapper() {
  # Copy scripts/jarvis-self-update → /usr/local/sbin/, root-owned 0755.
  # services/update_service.py invokes this via `sudo -n` to launch the
  # upgrade installer in a transient systemd unit. Without this wrapper +
  # its NOPASSWD sudoers grant, the update path hits a sudo password
  # prompt with no TTY to answer it and silently fails — see prod kitchen
  # node v0.1.49 → v0.1.50 incident, May 2026.
  local src="${INSTALL_DIR}/scripts/jarvis-self-update"
  local dest="/usr/local/sbin/jarvis-self-update"
  if [ ! -f "$src" ]; then
    warn "scripts/jarvis-self-update missing — mobile-triggered updates will fail with sudo password prompt"
    return
  fi
  install -o root -g root -m 0755 "$src" "$dest"
  success "Installed ${dest}"
}

install_alsa_store_wrapper() {
  # Copy scripts/jarvis-alsa-store → /usr/local/sbin/, root-owned 0755.
  # Lets jarvis-node persist amixer changes to /var/lib/alsa/asound.state
  # without a sudo password prompt. Without this wrapper any runtime
  # mixer tuning (volume baseline drift correction, hand-applied amixer
  # ops during debug) evaporates on the next reboot because alsa-restore
  # reloads the install-time baseline — see prod kitchen node calibration
  # session, May 2026.
  local src="${INSTALL_DIR}/scripts/jarvis-alsa-store"
  local dest="/usr/local/sbin/jarvis-alsa-store"
  if [ ! -f "$src" ]; then
    warn "scripts/jarvis-alsa-store missing — runtime mixer changes will not persist across reboot"
    return
  fi
  install -o root -g root -m 0755 "$src" "$dest"
  success "Installed ${dest}"
}

install_sudoers() {
  local sudoers_path="/etc/sudoers.d/jarvis-node"
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# Generated by jarvis-node install.sh — do not edit by hand.
# Grants ${SERVICE_USER} NOPASSWD access to the privileged binaries the
# jarvis-node service needs at runtime (factory reset, self-restart,
# AP-mode provisioning).
${SERVICE_USER} ALL=(root) NOPASSWD: /sbin/reboot, /usr/sbin/reboot
${SERVICE_USER} ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown
${SERVICE_USER} ALL=(root) NOPASSWD: /sbin/poweroff, /usr/sbin/poweroff
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart jarvis-node, /usr/bin/systemctl restart jarvis-node.service
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop NetworkManager, /usr/bin/systemctl start NetworkManager
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/nmcli
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop wpa_supplicant, /usr/bin/systemctl start wpa_supplicant
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop dnsmasq, /usr/bin/systemctl start dnsmasq
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/sbin/hostapd
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/sbin/dnsmasq
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/pkill, /usr/bin/killall, /usr/bin/pgrep
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/sbin/ip
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/local/sbin/jarvis-apt-install *
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/local/sbin/jarvis-post-install *
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/local/sbin/jarvis-self-update *
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/local/sbin/jarvis-alsa-store
EOF
  chmod 0440 "$tmp"
  if visudo -cf "$tmp" >/dev/null 2>&1; then
    mv "$tmp" "$sudoers_path"
    success "Installed ${sudoers_path}"
  else
    rm -f "$tmp"
    warn "sudoers.d/jarvis-node failed visudo validation — runtime privileged ops may fail"
  fi
}

# --- Create systemd service ---
create_service() {
  info "Creating systemd service..."

  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  local upgrade_backup="${unit_path}.upgrade-backup"

  # Clean up old provisioning service from previous setup versions
  if [ -f /etc/systemd/system/jarvis-provisioning.service ]; then
    systemctl stop jarvis-provisioning.service 2>/dev/null || true
    systemctl disable jarvis-provisioning.service 2>/dev/null || true
    rm -f /etc/systemd/system/jarvis-provisioning.service
  fi

  # Save the previous unit so start_service() can restore it on
  # rollback. Without this, a failed upgrade on a pre-migration
  # install would leave the new User=pi unit pointing at rolled-back
  # code that still expects to run as root.
  if [ -f "$unit_path" ]; then
    cp "$unit_path" "$upgrade_backup"
  fi

  local service_uid
  service_uid="$(id -u "${SERVICE_USER}")"

  # Use the service template shipped in the release
  if [ -f "${INSTALL_DIR}/setup/jarvis-node.service" ]; then
    sed -e "s|__VENV__|${INSTALL_DIR}/.venv|g" \
        -e "s|__PROJECT_DIR__|${INSTALL_DIR}|g" \
        -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
        -e "s|__SERVICE_HOME__|${SERVICE_HOME}|g" \
        -e "s|__SERVICE_UID__|${service_uid}|g" \
        "${INSTALL_DIR}/setup/jarvis-node.service" \
        > "$unit_path"
  else
    # Fallback if template is missing (older releases)
    cat > "$unit_path" <<EOF
[Unit]
Description=Jarvis Node Service
After=network-online.target
Wants=network-online.target
# Rate-limit settings live in [Unit] (since systemd v229) — having them
# under [Service] is silently ignored AND logs "Unknown key" on every
# daemon-reload.
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
ExecStart=${INSTALL_DIR}/.venv/bin/python -m scripts.main
WorkingDirectory=${INSTALL_DIR}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
Environment=HOME=${SERVICE_HOME}
Environment=XDG_RUNTIME_DIR=/run/user/${service_uid}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${INSTALL_DIR}
Environment=CONFIG_PATH=${INSTALL_DIR}/config.json
Environment=JARVIS_CONFIG_URL_STYLE=remote
SyslogIdentifier=jarvis-node

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"

  success "Systemd service created and enabled"
}

# --- Start the service ---
#
# After restart, poll `systemctl is-active` for HEALTH_TIMEOUT seconds. If
# the service doesn't come back up we treat that as a failed upgrade and
# restore from the pre-upgrade .bak so the node recovers on its own.
# Mobile-triggered updates depend on this — a node that never heartbeats
# again is a node stuck in "in_progress" until the CC sweeper gives up.
start_service() {
  info "Starting ${SERVICE_NAME}..."
  systemctl restart "${SERVICE_NAME}.service"

  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  local upgrade_backup="${unit_path}.upgrade-backup"

  # Only run the health-check + rollback dance during upgrades (when
  # there's a .bak to rollback TO). Fresh installs on a Pi Zero can
  # take >60s to boot Python + run migrations — a strict timeout here
  # would kill the installer before the service ever came up.
  local backup="${INSTALL_DIR}.bak"
  if [ ! -d "$backup" ]; then
    success "Service started (fresh install — skipping health check)"
    rm -f "$upgrade_backup"
    return 0
  fi

  local health_timeout=120
  local waited=0
  while [ "$waited" -lt "$health_timeout" ]; do
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
      success "Service started"
      rm -f "$upgrade_backup"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done

  warn "Service did not become active within ${health_timeout}s"
  warn "Rolling back to previous install at ${backup}"
  systemctl stop "${SERVICE_NAME}.service" || true
  rm -rf "${INSTALL_DIR}.failed"
  mv "$INSTALL_DIR" "${INSTALL_DIR}.failed"
  mv "$backup" "$INSTALL_DIR"
  # Restore the previous unit too — the new User=pi unit references
  # a service user the rolled-back code may not be set up to run as.
  if [ -f "$upgrade_backup" ]; then
    mv "$upgrade_backup" "$unit_path"
    systemctl daemon-reload
  fi
  systemctl restart "${SERVICE_NAME}.service"
  waited=0
  while [ "$waited" -lt "$health_timeout" ]; do
    if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
      warn "Rollback successful — node running previous version"
      exit 1
    fi
    sleep 3
    waited=$((waited + 3))
  done
  error "Rollback also failed — manual intervention required"
}

# --- Verify ---
verify() {
  info "Verifying installation..."

  local python="${INSTALL_DIR}/.venv/bin/python"
  info "Python: $($python --version)"
  info "Version: $(cat "${INSTALL_DIR}/VERSION")"

  # Quick smoke test
  if $python -c "import sqlalchemy; import paho.mqtt.client; import fastapi" 2>/dev/null; then
    success "Core packages verified"
  else
    warn "Some packages may be missing — check logs"
  fi
}

# --- Print success ---
print_success() {
  printf "\n"
  printf "${GREEN}${BOLD}Jarvis Node installed successfully!${NC}\n"
  printf "\n"
  printf "  Version:  %s\n" "$VERSION"
  printf "  Location: %s\n" "$INSTALL_DIR"
  printf "\n"

  if [ "${NEEDS_REBOOT:-0}" -eq 1 ]; then
    printf "  ${BOLD}Rebooting now${NC} to activate kernel overlays (TLV320AIC3104\n"
    printf "  audio codec, SPI, I2C). The node will start automatically in\n"
    printf "  provisioning mode after reboot. Connect with the Jarvis mobile\n"
    printf "  app to complete setup.\n"
  else
    printf "  The node is running in provisioning mode.\n"
    printf "  Connect with the Jarvis mobile app to complete setup.\n"
  fi

  printf "\n"
  printf "  View logs:\n"
  printf "    sudo journalctl -u ${SERVICE_NAME} -f\n"
  printf "\n"
}

# --- Main ---
main() {
  NEEDS_REBOOT=0

  preflight
  get_version
  install_apt_deps

  # Set up the service user (groups, lingering, ~/.jarvis) BEFORE the
  # heavy steps so alembic + any other key-writing code in the install
  # pipeline writes into ${SERVICE_HOME}/.jarvis from the start, not
  # into /root/.jarvis (which would orphan keys after the user switch).
  setup_service_user

  # Stop jarvis-node before the heavy steps (download / extract / rebuild
  # venv / pip) to free RAM on 512 MB boards where the running service and
  # a concurrent install will OOM-kill each other.
  #
  # Safety: only stop if we're NOT inside jarvis-node's own service cgroup.
  # Under cgroups v2 a `systemctl stop jarvis-node` tears down the whole
  # cgroup including any installer bash that was spawned as a child of the
  # service (the pre-v0.1.11 launch path used setsid, which doesn't detach
  # from cgroups). The v0.1.11+ launcher uses `systemd-run --unit=` so the
  # installer lands in its own "jarvis-node-update.service" cgroup — safe
  # to stop jarvis-node.service from there.
  #
  # Match "jarvis-node.service" (not bare "jarvis-node") so we don't
  # false-positive on "jarvis-node-update.service" — the isolated unit
  # systemd-run creates also contains "jarvis-node" as a substring.
  if grep -qE "/jarvis-node\\.service(\$|/)" /proc/self/cgroup 2>/dev/null; then
    info "Installer is inside jarvis-node.service's cgroup — leaving service running"
  elif systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    info "Stopping ${SERVICE_NAME} to free memory for the upgrade..."
    systemctl stop "${SERVICE_NAME}.service" || true
  fi

  download_and_extract
  configure_audio
  setup_config
  rebuild_venv
  setup_database
  register_commands
  migrate_to_pi_home
  chown_install_dir
  install_apt_wrapper
  install_post_install_wrapper
  install_self_update_wrapper
  install_alsa_store_wrapper
  install_sudoers
  create_service

  # Kernel overlays (e.g. hifiberry-dac) and modprobe options only take effect
  # on the next boot. Starting the service now would make it race against a
  # half-configured ALSA stack: asound.conf references card names that don't
  # exist yet, PyAudio retries for minutes, provisioning API runs but slowly.
  # Reboot instead — systemctl enable already ran, so the service comes up
  # clean on the next boot with all overlays in place.
  if [ "${NEEDS_REBOOT:-0}" -eq 1 ]; then
    verify
    print_success
    info "Rebooting in 5 seconds to finish setup..."
    sleep 5
    reboot
    return
  fi

  start_service
  verify
  print_success

  # On resource-constrained devices (Pi Zero 2W, 512MB RAM) the update
  # process (pip install, tarball extraction) can leave heavy swap/cache
  # pressure.  A reboot after a successful upgrade gives the cleanest
  # runtime state.  Fresh installs already reboot above when NEEDS_REBOOT
  # is set; this covers the upgrade path.
  local backup="${INSTALL_DIR}.bak"
  if [ -d "$backup" ] || [ -d "${INSTALL_DIR}.failed" ]; then
    info "Upgrade complete — rebooting to reclaim resources..."
    sleep 3
    reboot
  fi
}

main
