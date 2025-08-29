import os
import json

from dotenv import load_dotenv

load_dotenv()


class Config:
    _config_json = None

    @staticmethod
    def _load_config():
        config_path = os.getenv("CONFIG_PATH")
        with open(config_path) as f:
            Config._config_json = json.load(f)

    @staticmethod
    def get(key: str, default: str = None):
        Config._load_config()
        return Config._config_json.get(key, default)
