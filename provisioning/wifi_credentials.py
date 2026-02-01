"""
Encrypted WiFi credential storage for provisioning.

Stores WiFi credentials separately from the main secrets database,
using Fernet encryption with the existing secrets.key.
"""

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from utils.encryption_utils import get_encryption_key, get_secret_dir, initialize_encryption_key


def _get_credentials_file() -> Path:
    """Get the path to the encrypted WiFi credentials file."""
    secret_dir = get_secret_dir()
    return secret_dir / "wifi_credentials.enc"


def save_wifi_credentials(ssid: str, password: str) -> None:
    """
    Save WiFi credentials to encrypted file.

    Args:
        ssid: WiFi network SSID
        password: WiFi network password
    """
    # Ensure encryption key exists
    initialize_encryption_key()
    key = get_encryption_key()
    fernet = Fernet(key)

    credentials = {"ssid": ssid, "password": password}
    plaintext = json.dumps(credentials).encode("utf-8")
    encrypted = fernet.encrypt(plaintext)

    credentials_file = _get_credentials_file()
    credentials_file.parent.mkdir(mode=0o700, exist_ok=True)

    with open(credentials_file, "wb") as f:
        f.write(encrypted)
    os.chmod(credentials_file, 0o600)


def load_wifi_credentials() -> Optional[tuple[str, str]]:
    """
    Load WiFi credentials from encrypted file.

    Returns:
        Tuple of (ssid, password) if credentials exist, None otherwise.
    """
    credentials_file = _get_credentials_file()

    if not credentials_file.exists():
        return None

    try:
        key = get_encryption_key()
        fernet = Fernet(key)

        with open(credentials_file, "rb") as f:
            encrypted = f.read()

        decrypted = fernet.decrypt(encrypted)
        credentials = json.loads(decrypted.decode("utf-8"))

        return (credentials["ssid"], credentials["password"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def clear_wifi_credentials() -> None:
    """Remove stored WiFi credentials."""
    credentials_file = _get_credentials_file()

    if credentials_file.exists():
        credentials_file.unlink()
