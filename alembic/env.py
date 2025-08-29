import sys
import os
from logging.config import fileConfig

from sqlalchemy import pool, create_engine, event
from alembic import context
from sqlalchemy.dialects.sqlite import pysqlite
from db import DATABASE_URL


# Patch to remove unsupported deterministic argument for pysqlcipher3
def patched_on_connect(conn):
    def regexp(a, b):
        import re
        return re.search(a, b) is not None
    conn.create_function("regexp", 2, regexp)  # Removed deterministic=True

pysqlite.SQLiteDialect_pysqlite.on_connect = lambda self: patched_on_connect

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import Base

# Alembic Config
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
config.set_main_option(
    "sqlalchemy.url",
    DATABASE_URL
)

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = create_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False, "detect_types": 0}
    )

    # # Ensure PRAGMAs are applied after connect
    # @event.listens_for(connectable, "connect")
    # def set_sqlcipher_pragma(dbapi_connection, connection_record):
    #     cursor = dbapi_connection.cursor()
    #     cursor.execute(f"PRAGMA key='{MASTER_KEY}';")
    #     cursor.execute("PRAGMA cipher_page_size = 4096;")
    #     cursor.execute("PRAGMA kdf_iter = 256000;")
    #     cursor.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512;")
    #     cursor.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
    #     cursor.close()

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()