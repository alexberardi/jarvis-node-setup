import os
from pathlib import Path
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