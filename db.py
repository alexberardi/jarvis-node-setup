import os
import secrets
from pathlib import Path

import sqlcipher3
from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

load_dotenv()


def _get_or_create_db_key() -> str:
    """Read the DB encryption key from ~/.jarvis/db.key, creating it if absent."""
    secret_dir = Path(os.environ.get("JARVIS_SECRET_DIRECTORY",
                                     str(Path.home() / ".jarvis")))
    secret_dir = Path(os.path.expandvars(os.path.expanduser(str(secret_dir))))
    key_file = secret_dir / "db.key"

    if key_file.exists():
        return key_file.read_text().strip()

    # Generate a new key and persist it
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    key = secrets.token_hex(32)
    key_file.write_text(key)
    os.chmod(key_file, 0o600)
    return key


MASTER_KEY = os.getenv("JARVIS_MASTER_KEY") or _get_or_create_db_key()
DB_PATH = os.getenv("JARVIS_NODE_DB", "./jarvis_node.db")

# Using sqlcipher3 as the DBAPI module with SQLAlchemy's sqlite dialect
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    module=sqlcipher3,
    connect_args={"check_same_thread": False, "detect_types": 0},
)


@event.listens_for(engine, "connect")
def _set_sqlcipher_key(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA key = '{MASTER_KEY}'")
    cursor.execute("PRAGMA cipher_compatibility = 4")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)