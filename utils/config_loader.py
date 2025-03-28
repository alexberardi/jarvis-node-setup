import os
import json


class Config:
    _config_json = None

    @staticmethod
    def _load_config():
        CONFIG_PATH = os.path.expanduser(
            "~/projects/jarvis-node-setup/config.json"
        )
        with open(CONFIG_PATH) as f:
            Config._config_json = json.load(f)

    @staticmethod
    def get(key: str, default: str = None):
        Config._load_config()
        return Config._config_json.get(key, default)
