#!/usr/bin/env python3
"""
macOS Development Script for Jarvis Node
Allows running different components for testing and development
"""

import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

def setup_environment():
    """Setup environment for development"""
    project_root = Path(__file__).parent
    sys.path.insert(0, str(project_root))
    load_dotenv()


def run_voice_listener():
    """Run the voice listener component"""
    logger.info("Starting voice listener", note="Requires microphone and wake word detection. Press Ctrl+C to stop")

    try:
        subprocess.run([
            sys.executable, "scripts/voice_listener.py"
        ], check=True)
    except KeyboardInterrupt:
        logger.info("Voice listener stopped")
    except subprocess.CalledProcessError as e:
        logger.error("Voice listener failed", error=str(e))


def run_mqtt_listener():
    """Run the MQTT listener component"""
    logger.info("Starting MQTT listener", note="Requires MQTT broker to be running. Press Ctrl+C to stop")

    try:
        subprocess.run([
            sys.executable, "scripts/mqtt_tts_listener.py"
        ], check=True)
    except KeyboardInterrupt:
        logger.info("MQTT listener stopped")
    except subprocess.CalledProcessError as e:
        logger.error("MQTT listener failed", error=str(e))


def run_speech_to_text():
    """Run speech-to-text test"""
    logger.info("Testing speech-to-text", note="Requires microphone. Speak after the prompt, then press Ctrl+C to stop")

    try:
        subprocess.run([
            sys.executable, "scripts/speech_to_text.py"
        ], check=True)
    except KeyboardInterrupt:
        logger.info("Speech-to-text test stopped")
    except subprocess.CalledProcessError as e:
        logger.error("Speech-to-text test failed", error=str(e))


def run_text_to_speech():
    """Run text-to-speech test"""
    logger.info("Testing text-to-speech")

    try:
        subprocess.run([
            sys.executable, "scripts/text_to_speech.py"
        ], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Text-to-speech test failed", error=str(e))


def run_device_discovery():
    """Run device discovery test"""
    logger.info("Running device discovery")

    try:
        subprocess.run([
            sys.executable, "scripts/scan_and_save_devices.py"
        ], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Device discovery failed", error=str(e))


def run_show_devices():
    """Show discovered devices"""
    logger.info("Showing discovered devices")

    try:
        subprocess.run([
            sys.executable, "scripts/show_discovered_devices.py"
        ], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Show devices failed", error=str(e))


def run_tests():
    """Run the test suite"""
    logger.info("Running tests")

    try:
        subprocess.run([
            sys.executable, "run_tests_macos.py"
        ], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Tests failed", error=str(e))


def run_full_system():
    """Run the full Jarvis Node system"""
    logger.info("Starting full Jarvis Node system", note="Runs both voice and MQTT listeners. Press Ctrl+C to stop")

    try:
        subprocess.run([
            sys.executable, "scripts/main.py"
        ], check=True)
    except KeyboardInterrupt:
        logger.info("Full system stopped")
    except subprocess.CalledProcessError as e:
        logger.error("Full system failed", error=str(e))


def main():
    """Main entry point"""
    setup_environment()
    
    parser = argparse.ArgumentParser(
        description="Jarvis Node macOS Development Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python dev_macos.py voice          # Run voice listener
  python dev_macos.py mqtt           # Run MQTT listener
  python dev_macos.py stt            # Test speech-to-text
  python dev_macos.py tts            # Test text-to-speech
  python dev_macos.py discover       # Run device discovery
  python dev_macos.py devices        # Show discovered devices
  python dev_macos.py tests          # Run test suite
  python dev_macos.py full           # Run full system
        """
    )
    
    parser.add_argument(
        'component',
        choices=['voice', 'mqtt', 'stt', 'tts', 'discover', 'devices', 'tests', 'full'],
        help='Component to run'
    )
    
    args = parser.parse_args()
    
    # Component mapping
    components = {
        'voice': run_voice_listener,
        'mqtt': run_mqtt_listener,
        'stt': run_speech_to_text,
        'tts': run_text_to_speech,
        'discover': run_device_discovery,
        'devices': run_show_devices,
        'tests': run_tests,
        'full': run_full_system
    }
    
    # Run the selected component
    components[args.component]()


if __name__ == "__main__":
    main() 