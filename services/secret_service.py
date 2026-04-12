from __future__ import annotations

from typing import TYPE_CHECKING

from db import SessionLocal
from models.secret import Secret
from repositories.secrets_repository import SecretRepository

if TYPE_CHECKING:
    from core.ijarvis_secret import IJarvisSecret

def set_secret(key: str, value: str, scope: str, value_type: str = "string", user_id: int | None = None):
    # Validate scope
    allowed_scopes = {"integration", "node", "user"}
    if scope not in allowed_scopes:
        raise ValueError(f"Invalid scope '{scope}'. Must be one of {allowed_scopes}.")

    if scope == "user" and user_id is None:
        raise ValueError(f"user_id is required for scope='user' (key={key})")

    # Validate value_type
    allowed_types = {"int", "string", "bool"}
    if value_type not in allowed_types:
        raise ValueError(f"Invalid value_type '{value_type}'. Must be one of {allowed_types}.")

    # Convert value according to type
    store_value = value
    if value_type == "int":
        try:
            store_value = str(int(value))
        except (TypeError, ValueError) as e:
            raise ValueError(f"Value '{value}' could not be converted to int: {e}")
    elif value_type == "bool":
        if isinstance(value, bool):
            store_value = "true" if value else "false"
        else:
            vstr = str(value).strip().lower()
            if vstr not in ("true", "false"):
                raise ValueError(f"Boolean value must be 'true' or 'false', got '{value}'.")
            store_value = vstr
    # else string: store as is

    with SessionLocal() as session:
        repo = SecretRepository(session)
        repo.add_or_update(key, store_value, scope, value_type, user_id=user_id)
        session.commit()


def get_secret(key: str, scope: str, user_id: int | None = None) -> Secret:
    with SessionLocal() as session:
        repo = SecretRepository(session)
        secret = repo.get(key, scope, user_id=user_id)
        return secret

def get_secret_value(key: str, scope: str, user_id: int | None = None):
    secret = get_secret(key, scope, user_id=user_id)
    return secret.value if secret else None

def get_secret_value_int(key: str, scope: str, user_id: int | None = None):
    secret = get_secret(key, scope, user_id=user_id)
    try:
        return int(secret.value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"The stored {key} is not a number")

def delete_secret(key: str, scope: str, user_id: int | None = None):
    with SessionLocal() as session:
        repo = SecretRepository(session)
        repo.delete(key, scope, user_id=user_id)
        session.commit()

def get_all_secrets(scope: str, user_id: int | None = None) -> list[Secret]:
    with SessionLocal() as session:
        repo = SecretRepository(session)
        secrets = repo.get_all(scope, user_id=user_id)
        return secrets


def ensure_secret_exists(key: str, scope: str, value_type: str) -> None:
    """Insert a secret row with empty value if it doesn't already exist.

    Used to pre-seed the DB with command-declared secrets so that
    scope/value_type metadata is available before the user sets a value.
    Skips user-scoped secrets (no user exists at seed time).
    """
    if scope == "user":
        return  # user-scoped secrets are created when a user configures them
    with SessionLocal() as session:
        repo = SecretRepository(session)
        existing = repo.get(key, scope)
        if existing is None:
            repo.add_or_update(key, "", scope, value_type)
            session.commit()


def get_secret_scope(key: str) -> str | None:
    """Look up the scope of a secret by key.

    Checks user scope first (any user_id) since it's the most specific,
    then integration and node. Returns the scope string or None if not found.
    """
    with SessionLocal() as session:
        # Check user scope (any user_id — we just need to know the scope exists)
        user_secret = session.query(Secret).filter_by(key=key, scope="user").first()
        if user_secret is not None:
            return "user"
        repo = SecretRepository(session)
        for scope in ("node", "integration"):
            secret = repo.get(key, scope)
            if secret is not None:
                return scope
    return None


def seed_command_secrets(secrets: list[IJarvisSecret]) -> int:
    """Pre-seed the DB with secrets from a command's required_secrets.

    Only inserts rows that don't already exist (never overwrites values).
    Skips user-scoped secrets (no user exists at seed time).
    Returns the number of new rows inserted.
    """
    inserted: int = 0
    with SessionLocal() as session:
        repo = SecretRepository(session)
        for secret in secrets:
            if secret.scope == "user":
                continue  # user-scoped secrets created per-user at config time
            existing = repo.get(secret.key, secret.scope)
            if existing is None:
                repo.add_or_update(secret.key, "", secret.scope, secret.value_type)
                inserted += 1
        if inserted > 0:
            session.commit()
    return inserted
