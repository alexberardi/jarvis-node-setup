import json
import requests


class RestClient:
    _config = None

    @staticmethod
    def _load_config():
        with open("config.json", "r") as f:
            RestClient._config = json.load(f)

    @staticmethod
    def post(url: str, data=None, files=None, timeout=10):
        RestClient._load_config()
        headers = {
            "X-Node-ID": RestClient._config.get("node_id", ""),
            "X-API-Key": RestClient._config.get("api_key", ""),
        }

        request_args = {"headers": headers, "timeout": timeout}

        if data is not None:
            print("THERE IS JSON")
            request_args["json"] = data

        if files is not None:
            print("THERE ARE FILES")
            request_args["files"] = files

        try:
            response = requests.post(url, **request_args)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"[RestClient] Error during POST to {url}: {e}")
            return None
