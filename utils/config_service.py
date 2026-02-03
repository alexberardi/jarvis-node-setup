import os
import json
from typing import Any, Dict, Optional

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class Config:
    _config_json: Optional[Dict[str, Any]] = None

    @staticmethod
    def _load_config() -> None:
        raw_path: str = os.environ.get('CONFIG_PATH', '')
        # Expand shell variables ($HOME) and user paths (~)
        config_path = os.path.expandvars(os.path.expanduser(raw_path))
        try:
            with open(config_path) as f:
                Config._config_json = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("Error loading config", path=config_path)
            Config._config_json = None

    @staticmethod
    def get_str(key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a string value from config"""
        Config._load_config()
        if Config._config_json is None:
            return default
        value = Config._config_json.get(key, default)
        return str(value) if value is not None else None

    @staticmethod
    def get_int(key: str, default: int) -> int:
        """Get an integer value from config"""
        Config._load_config()
        if Config._config_json is None:
            return default
        value = Config._config_json.get(key, default)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def get_bool(key: str, default: Optional[bool] = None) -> Optional[bool]:
        """Get a boolean value from config"""
        Config._load_config()
        if Config._config_json is None:
            return default
        value = Config._config_json.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            # Empty string is False, non-empty string is True
            return bool(value) and value.lower() not in ('false', '0', 'no', 'off')
        if isinstance(value, (int, float)):
            return bool(value)
        # For any other type, convert to string and check
        return bool(value)

    @staticmethod
    def get_float(key: str, default: float) -> float:
        """Get a float value from config"""
        Config._load_config()
        if Config._config_json is None:
            return default
        value = Config._config_json.get(key, default)
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # Legacy method for backward compatibility
    @staticmethod
    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        """Legacy method - use type-specific getters instead"""
        return Config.get_str(key, default)
