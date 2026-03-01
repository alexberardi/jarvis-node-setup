#!/usr/bin/env python3
"""List available audio input devices to help find the right mic_device_index."""

import pyaudio


def main() -> None:
    pa = pyaudio.PyAudio()
    default_input = pa.get_default_input_device_info()

    print(f"\nDefault input device: [{default_input['index']}] {default_input['name']}")
    print(f"  Sample rate: {int(default_input['defaultSampleRate'])} Hz")
    print(f"  Max input channels: {default_input['maxInputChannels']}\n")

    print("All input devices:")
    print("-" * 60)

    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            marker = " <-- default" if i == default_input["index"] else ""
            print(f"  [{i}] {info['name']}{marker}")
            print(f"      Channels: {info['maxInputChannels']}  "
                  f"Sample rate: {int(info['defaultSampleRate'])} Hz")

    pa.terminate()
    print("\nTo use a specific device, add \"mic_device_index\": <index> to your config JSON.")
    print("Omit mic_device_index to use the system default.\n")


if __name__ == "__main__":
    main()
