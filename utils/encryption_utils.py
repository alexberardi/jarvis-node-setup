import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from cryptography.fernet import Fernet

def get_secret_dir() -> Path:
    raw_path = os.environ.get("JARVIS_SECRET_DIRECTORY", str(Path.home() / ".jarvis"))
    # Expand shell variables ($HOME) and user paths (~)
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    return Path(expanded)

def get_key_file() -> Path:
    secret_dir = get_secret_dir()
    raw_path = os.environ.get("JARVIS_KEY_FILE", str(secret_dir / "secrets.key"))
    # Expand shell variables ($HOME) and user paths (~)
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    return Path(expanded)

def get_encryption_key() -> bytes:
    """Read the Fernet key from the configured key file and return as bytes."""
    key_file = get_key_file()
    if not key_file.exists():
        raise FileNotFoundError(f"Encryption key not found at {key_file}. Run initialize_encryption_key() first.")
    with open(key_file, "rb") as f:
        return f.read().strip()

def initialize_encryption_key() -> bytes:
    """Create the Fernet key file if it doesn't exist. Returns the key as bytes."""
    secret_dir = get_secret_dir()
    key_file = get_key_file()
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    if key_file.exists():
        with open(key_file, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(key_file, "wb") as f:
        f.write(key)
    os.chmod(key_file, 0o600)
    return key


# K2 key storage
K2_ENCRYPTED_FILENAME = "k2.enc"
K2_METADATA_FILENAME = "k2_metadata.json"


class K2Data(NamedTuple):
    """K2 key data with metadata."""
    k2: bytes  # Raw 32-byte key
    kid: str  # Key identifier
    created_at: datetime


def get_k2_file() -> Path:
    """Get path to encrypted K2 file."""
    return get_secret_dir() / K2_ENCRYPTED_FILENAME


def get_k2_metadata_file() -> Path:
    """Get path to K2 metadata file."""
    return get_secret_dir() / K2_METADATA_FILENAME


def save_k2(k2_base64url: str, kid: str, created_at: datetime) -> None:
    """
    Save K2 key encrypted with K1 (Fernet).

    Args:
        k2_base64url: Base64url-encoded 32-byte K2 key
        kid: Key identifier (e.g., "k2-2026-01")
        created_at: When the key was created

    Raises:
        ValueError: If K2 is not exactly 32 bytes after decoding
        FileNotFoundError: If K1 (encryption key) doesn't exist
    """
    # Decode from base64url
    try:
        k2_raw = base64.urlsafe_b64decode(k2_base64url)
    except Exception as e:
        raise ValueError(f"Invalid base64url encoding: {e}") from e

    # Validate K2 length (must be exactly 32 bytes for AES-256)
    if len(k2_raw) != 32:
        raise ValueError(f"K2 must be exactly 32 bytes, got {len(k2_raw)}")

    # Encrypt K2 with K1
    k1 = get_encryption_key()
    fernet = Fernet(k1)
    encrypted_k2 = fernet.encrypt(k2_raw)

    # Write encrypted K2
    secret_dir = get_secret_dir()
    secret_dir.mkdir(mode=0o700, exist_ok=True)

    k2_file = get_k2_file()
    with open(k2_file, "wb") as f:
        f.write(encrypted_k2)
    os.chmod(k2_file, 0o600)

    # Write metadata
    metadata = {
        "kid": kid,
        "created_at": created_at.isoformat(),
    }
    metadata_file = get_k2_metadata_file()
    with open(metadata_file, "w") as f:
        json.dump(metadata, f, indent=2)
    os.chmod(metadata_file, 0o600)


def get_k2() -> K2Data | None:
    """
    Load and decrypt K2 key.

    Returns:
        K2Data with raw key, kid, and created_at, or None if K2 not stored

    Raises:
        FileNotFoundError: If K1 (encryption key) doesn't exist
        ValueError: If metadata is invalid
    """
    k2_file = get_k2_file()
    metadata_file = get_k2_metadata_file()

    if not k2_file.exists() or not metadata_file.exists():
        return None

    # Read and decrypt K2
    k1 = get_encryption_key()
    fernet = Fernet(k1)

    with open(k2_file, "rb") as f:
        encrypted_k2 = f.read()

    k2_raw = fernet.decrypt(encrypted_k2)

    # Read metadata
    with open(metadata_file, "r") as f:
        metadata = json.load(f)

    created_at = datetime.fromisoformat(metadata["created_at"])

    return K2Data(k2=k2_raw, kid=metadata["kid"], created_at=created_at)


def has_k2() -> bool:
    """Check if K2 is stored."""
    return get_k2_file().exists() and get_k2_metadata_file().exists()


def clear_k2() -> None:
    """Remove K2 files (for re-provisioning)."""
    k2_file = get_k2_file()
    metadata_file = get_k2_metadata_file()

    if k2_file.exists():
        k2_file.unlink()
    if metadata_file.exists():
        metadata_file.unlink()