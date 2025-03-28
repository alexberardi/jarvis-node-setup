import json
import os
import paho.mqtt.client as mqtt
import subprocess

CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

# Load config
with open(CONFIG_PATH) as f:
    config = json.load(f)

TOPIC = config["mqtt_topic"]
MQTT_BROKER = config["mqtt_broker"]
MQTT_PORT = config.get("mqtt_port", 1883)
MQTT_USERNAME = config.get("mqtt_username")  # unused right now
MQTT_PASSWORD = config.get("mqtt_password")  # unused right now

PATH_TO_PROJECT = "~/projects/jarvis-node-setup"
CHIME_PATH = os.path.expanduser(f"{PATH_TO_PROJECT}/sounds/chime.wav")
PLAY_CHIME = config.get("play_chime", True)


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
