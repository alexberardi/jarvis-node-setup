#!/usr/bin/env python3
"""Text-based entry point for Jarvis — type instead of speak.

Bypasses wake word detection and audio capture entirely.
No audio dependencies required (no pyaudio, pvporcupine, numpy, scipy).

Usage:
    python scripts/keyboard_listener.py
    python scripts/keyboard_listener.py --speaker-user-id 42

Commands:
    /new    - Start a fresh conversation (clear context)
    quit, exit, bye, Ctrl+C, Ctrl+D - Exit
"""

import argparse
import os
import sys
import threading

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_log_client import JarvisLogger

from clients.responses.jarvis_command_center import ValidationRequest
from utils.command_execution_service import CommandExecutionService
from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node-keyboard")

EXIT_PHRASES = {"quit", "exit", "bye"}


def keyboard_validation_handler(validation: ValidationRequest) -> str:
    """Handle validation requests by prompting the user via keyboard.

    Args:
        validation: The validation request from the command center

    Returns:
        The user's typed response
    """
    print(f"\nJarvis needs clarification: {validation.question}")
    if validation.options:
        for i, option in enumerate(validation.options, 1):
            print(f"  {i}. {option}")
    try:
        return input("Your answer: ")
    except EOFError:
        return ""


def main() -> None:
    """Run the keyboard-based Jarvis REPL."""
    parser = argparse.ArgumentParser(description="Jarvis Keyboard Mode")
    parser.add_argument(
        "--speaker-user-id", type=int, default=None,
        help="Simulate a recognized speaker (pass a user ID from jarvis-auth)",
    )
    args = parser.parse_args()

    speaker_user_id: int | None = args.speaker_user_id

    print("Jarvis Keyboard Mode")
    if speaker_user_id is not None:
        print(f"Speaker: user_id={speaker_user_id}")
    print("Type your commands. Type 'quit', 'exit', or 'bye' to stop.")
    print("Type '/new' to start a fresh conversation.\n")

    # Initialize agent scheduler so HA entity area data is available
    try:
        from services.agent_scheduler_service import initialize_agent_scheduler
        initialize_agent_scheduler()
    except Exception as e:
        print(f"Agent scheduler not started: {e}")

    # Start MQTT listener in background (handles TTS, settings requests, config pushes)
    mqtt_enabled: bool = Config.get_bool("mqtt_enabled", True) is not False
    if mqtt_enabled:
        try:
            from scripts.mqtt_tts_listener import start_mqtt_listener
            from utils.music_assistant_service import DummyMusicAssistantService

            mqtt_thread = threading.Thread(
                target=start_mqtt_listener,
                args=(DummyMusicAssistantService(),),
                daemon=True,
            )
            mqtt_thread.start()
            print("MQTT listener started (settings/config push enabled)")
        except Exception as e:
            print(f"MQTT listener not started: {e}")

    service = CommandExecutionService()
    active_conversation_id: str | None = None

    # Warm up the LLM by sending a throwaway request through the full
    # pipeline (tool registration → system prompt → KV cache).  This
    # primes llama.cpp's prefix cache so the first real command is fast.
    print("Warming up LLM...", end=" ", flush=True)
    service.process_voice_command(
        "hello",
        speaker_user_id=speaker_user_id,
    )
    print("ready.\n")

    while True:
        try:
            user_input = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        stripped = user_input.strip()

        if not stripped:
            continue

        if stripped.lower() in EXIT_PHRASES:
            print("Goodbye!")
            break

        if stripped == "/new":
            active_conversation_id = None
            print("(Starting fresh conversation)\n")
            continue

        # Route to continue or start based on active conversation
        if active_conversation_id:
            result = service.continue_conversation(
                active_conversation_id,
                stripped,
                validation_handler=keyboard_validation_handler,
            )
        else:
            result = service.process_voice_command(
                stripped,
                validation_handler=keyboard_validation_handler,
                speaker_user_id=speaker_user_id,
            )

        # Display response
        message = result.get("message", "An error occurred")
        print(f"Jarvis: {message}\n")

        # Manage conversation state based on command signals
        if result.get("wait_for_input"):
            if result.get("clear_history"):
                active_conversation_id = None
            else:
                active_conversation_id = result.get("conversation_id")
        else:
            active_conversation_id = None


if __name__ == "__main__":
    main()
