import sys
import os
from logging.config import fileConfig

import sqlcipher3
from sqlalchemy import pool, create_engine, event
from alembic import context
from db import MASTER_KEY, DB_PATH

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from models import Base

# Alembic Config
config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False so JarvisLogger (set up before
    # migrations) keeps emitting after this call. Default True silences it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
config.set_main_option(
    "sqlalchemy.url",
    f"sqlite:///{DB_PATH}"
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
        f"sqlite:///{DB_PATH}",
        module=sqlcipher3,
        poolclass=pool.NullPool,
        connect_args={"check_same_thread": False, "detect_types": 0},
    )

    @event.listens_for(connectable, "connect")
    def _set_sqlcipher_key(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA key = '{MASTER_KEY}'")
        cursor.execute("PRAGMA cipher_compatibility = 4")
        cursor.close()

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()