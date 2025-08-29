import os

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

MASTER_KEY = os.getenv("JARVIS_MASTER_KEY", "default-key")
DB_PATH = os.getenv("JARVIS_SECRET_DB", "./secrets.db")

# Using pysqlcipher3 dialect for SQLAlchemy
DATABASE_URL = f"sqlite+pysqlcipher://:{MASTER_KEY}@/{DB_PATH}?cipher=aes-256-cbc&kdf_iter=256000"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "detect_types": 0}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)