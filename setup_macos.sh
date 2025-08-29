#!/bin/bash

set -e

echo "🍎 [0/5] Setting up Jarvis Node for macOS..."

echo "🔧 [1/5] Checking prerequisites..."
# Check if Homebrew is installed
if ! command -v brew &> /dev/null; then
    echo "❌ Homebrew not found. Please install Homebrew first:"
    echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
fi

echo "📦 [2/5] Installing system dependencies..."
brew install python@3.11 portaudio sox ffmpeg espeak mosquitto

echo "🐍 [3/5] Creating Python venv and installing requirements..."
if [ ! -d venv ]; then
    python3.11 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "📝 [4/5] Preparing config..."
if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "📁 config.json created from example — be sure to update it."
else
    echo "✅ config.json already exists, skipping."
fi

echo "🎧 [5/5] Setting up audio configuration..."
# Create macOS-specific audio config
mkdir -p ~/.config/jarvis-node

cat > ~/.config/jarvis-node/audio_config.json <<EOF
{
  "platform": "macos",
  "audio_output": "default",
  "audio_input": "default",
  "sample_rate": 48000,
  "channels": 1
}
EOF

echo "✅ macOS setup complete!"
echo ""
echo "🎯 To run Jarvis Node:"
echo "   source venv/bin/activate"
echo "   python scripts/main.py"
echo ""
echo "🎯 To run tests:"
echo "   source venv/bin/activate"
echo "   python -m pytest tests/"
echo ""
echo "⚠️  Note: Audio functionality will use macOS Core Audio instead of ALSA" 