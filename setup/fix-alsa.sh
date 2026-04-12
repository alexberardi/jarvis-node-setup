#!/bin/bash
# Apply softvol ALSA config for HifiBerry DAC + USB mic
# Run on Pi: sudo bash setup/fix-alsa.sh

set -e

# Auto-detect USB mic card number (same logic as pi.sh)
USB_MIC_CARD=$(arecord -l 2>/dev/null | grep -i "usb" | sed -n 's/.*card \([0-9]*\):.*/\1/p' | head -n 1)
if [[ -z "$USB_MIC_CARD" || ! "$USB_MIC_CARD" =~ ^[0-9]+$ ]]; then
    echo "WARNING: No USB mic detected, defaulting to card 2"
    USB_MIC_CARD=2
else
    echo "Detected USB mic on card $USB_MIC_CARD"
fi

cat > /etc/asound.conf << ALSA
defaults.pcm.card 0
defaults.pcm.device 0
defaults.ctl.card 0

pcm.softvol {
  type softvol
  slave.pcm "plughw:0,0"
  control {
    name "SoftMaster"
    card 0
  }
}

# Alias used by PiAudioProvider (aplay -D output)
pcm.output {
  type plug
  slave.pcm "softvol"
}

pcm.dsnoopmic {
  type dsnoop
  ipc_key 87654321
  slave {
    pcm "hw:${USB_MIC_CARD},0"
    channels 1
  }
}

pcm.!default {
  type asym
  playback.pcm "softvol"
  capture.pcm "dsnoopmic"
}
ALSA

echo "ALSA config written to /etc/asound.conf (mic card=$USB_MIC_CARD)"

# Create the SoftMaster control by playing a short test tone
speaker-test -c 1 -t sine -l 1 > /dev/null 2>&1 &
sleep 1
kill $! 2>/dev/null || true

# Set volume to 85%
amixer set SoftMaster 85% 2>/dev/null && echo "Volume set to 85%" || echo "Run speaker-test first to create SoftMaster control"
