import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.sqlite import pysqlite

load_dotenv()

# Patch to remove unsupported deterministic argument for pysqlcipher3
def patched_on_connect(conn):
    def regexp(a, b):
        import re
        return re.search(a, b) is not None
    conn.create_function("regexp", 2, regexp)  # Removed deterministic=True

pysqlite.SQLiteDialect_pysqlite.on_connect = lambda self: patched_on_connect


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

# Using pysqlcipher3 dialect for SQLAlchemy
DATABASE_URL = f"sqlite+pysqlcipher://:{MASTER_KEY}@/{DB_PATH}?cipher=aes-256-cbc&kdf_iter=256000"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "detect_types": 0}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)