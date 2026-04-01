"""Concrete StorageBackend for the node runtime.

Bridges the SDK's JarvisStorage facade to the node's SQLAlchemy-backed
repositories (command_data + secrets).

Registered once at startup via:
    from services.storage_backend import init_storage_backend
    init_storage_backend()
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis_command_sdk.storage import StorageBackend, set_backend

from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository
from services.secret_service import (
    delete_secret as _delete_secret,
    get_secret_value as _get_secret_value,
    set_secret as _set_secret,
)


class NodeStorageBackend(StorageBackend):
    """Storage backend backed by the node's encrypted SQLite database."""

    # -- Command data --

    def save(
        self,
        command_name: str,
        data_key: str,
        data: dict[str, Any],
        expires_at: datetime | None = None,
    ) -> None:
        with SessionLocal() as session:
            repo = CommandDataRepository(session)
            repo.save(command_name, data_key, data, expires_at)

    def get(self, command_name: str, data_key: str) -> dict[str, Any] | None:
        with SessionLocal() as session:
            repo = CommandDataRepository(session)
            return repo.get(command_name, data_key)

    def get_all(self, command_name: str) -> list[dict[str, Any]]:
        with SessionLocal() as session:
            repo = CommandDataRepository(session)
            return repo.get_all(command_name)

    def delete(self, command_name: str, data_key: str) -> bool:
        with SessionLocal() as session:
            repo = CommandDataRepository(session)
            return repo.delete(command_name, data_key)

    def delete_all(self, command_name: str) -> int:
        with SessionLocal() as session:
            repo = CommandDataRepository(session)
            return repo.delete_all(command_name)

    # -- Secrets --

    def get_secret(self, key: str, scope: str, user_id: int | None = None) -> str | None:
        value = _get_secret_value(key, scope, user_id=user_id)
        return str(value) if value is not None else None

    def set_secret(
        self, key: str, value: str, scope: str, value_type: str = "string",
        user_id: int | None = None,
    ) -> None:
        _set_secret(key, value, scope, value_type, user_id=user_id)

    def delete_secret(self, key: str, scope: str, user_id: int | None = None) -> None:
        _delete_secret(key, scope, user_id=user_id)


def init_storage_backend() -> None:
    """Register the node storage backend with the SDK. Call once at startup."""
    set_backend(NodeStorageBackend())
