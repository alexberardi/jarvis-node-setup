import json
import os
import httpx

CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

# Load config
with open(CONFIG_PATH) as f:
    config = json.load(f)

SERVER_URL = config["api_url"]
API_KEY = config["api_key"]

def send_voice(text: str):
    url = f"{SERVER_URL}/voice"
    headers = {"x-api-key": API_KEY}
    payload = {"text": text}

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        print("✅ Jarvis response:", response.json()["response"])
    except httpx.HTTPStatusError as e:
        print(f"❌ Server returned error: {e.response.status_code} {e.response.text}")
    except Exception as e:
        print(f"❌ Request failed: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python voice_sender.py \"your command here\"")
    else:
        send_voice(" ".join(sys.argv[1:]))

