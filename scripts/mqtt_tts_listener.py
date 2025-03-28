import json
import os
import paho.mqtt.client as mqtt
import subprocess
from utils.config_loader import Config


TOPIC = Config.get("mqtt_topic", "home/nodes/zero-office/tts")
MQTT_BROKER = Config("mqtt_broker", "10.0.0.173")
MQTT_PORT = Config.get("mqtt_port", 1883)
MQTT_USERNAME = Config.get("mqtt_username", "")  # unused right now
MQTT_PASSWORD = Config.get("mqtt_password", "")  # unused right now

PATH_TO_PROJECT = "~/projects/jarvis-node-setup"
CHIME_PATH = os.path.expanduser(f"{PATH_TO_PROJECT}/sounds/chime.wav")
PLAY_CHIME = Config.get("play_chime", True)


def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected with result code {rc}")
    client.subscribe(TOPIC)
    print(f"[MQTT] Subscribed to topic: {TOPIC}")


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        print(f"[MQTT] Received message: {payload}")

        if PLAY_CHIME and os.path.exists(CHIME_PATH):
            subprocess.run(["aplay", CHIME_PATH])

        subprocess.run(["espeak", payload])
    except Exception as e:
        print(f"[ERROR] {e}")


def main():
    client = mqtt.Client()

    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
