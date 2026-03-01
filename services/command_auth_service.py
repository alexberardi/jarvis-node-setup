"""Service for managing command authentication status.

Tracks which providers need (re-)authentication, allowing the mobile
app to prompt users and the node to know when credentials are stale.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from db import SessionLocal
from models.command_auth import CommandAuth


@dataclass
class CommandAuthStatus:
    """Read-only view of a provider's auth status."""

    provider: str
    needs_auth: bool
    auth_error: str | None
    last_checked_at: str | None
    last_authed_at: str | None


def get_auth_status(provider: str) -> CommandAuthStatus | None:
    """Get current auth status for a provider.

    Args:
        provider: Auth provider key (e.g., "home_assistant")

    Returns:
        CommandAuthStatus or None if provider has no entry
    """
    with SessionLocal() as session:
        row = session.query(CommandAuth).filter(
            CommandAuth.provider == provider,
        ).first()
        if not row:
            return None
        return CommandAuthStatus(
            provider=row.provider,
            needs_auth=bool(row.needs_auth),
            auth_error=row.auth_error,
            last_checked_at=row.last_checked_at,
            last_authed_at=row.last_authed_at,
        )


def set_needs_auth(provider: str, error: str | None = None) -> None:
    """Flag a provider as needing (re-)authentication.

    Called when a command detects auth failure (e.g., 401 from HA).

    Args:
        provider: Auth provider key
        error: Optional error message (e.g., "401 Unauthorized")
    """
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        row = session.query(CommandAuth).filter(
            CommandAuth.provider == provider,
        ).first()
        if row:
            row.needs_auth = 1
            row.auth_error = error
            row.last_checked_at = now
        else:
            row = CommandAuth(
                provider=provider,
                needs_auth=1,
                auth_error=error,
                last_checked_at=now,
            )
            session.add(row)
        session.commit()


def clear_auth_flag(provider: str) -> None:
    """Clear the re-auth flag after successful authentication.

    Called by store_auth_values() after storing new credentials.

    Args:
        provider: Auth provider key
    """
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        row = session.query(CommandAuth).filter(
            CommandAuth.provider == provider,
        ).first()
        if row:
            row.needs_auth = 0
            row.auth_error = None
            row.last_checked_at = now
            row.last_authed_at = now
        else:
            row = CommandAuth(
                provider=provider,
                needs_auth=0,
                auth_error=None,
                last_checked_at=now,
                last_authed_at=now,
            )
            session.add(row)
        session.commit()


def get_all_auth_statuses() -> list[CommandAuthStatus]:
    """Get auth status for all tracked providers.

    Returns:
        List of CommandAuthStatus for all providers in the table
    """
    with SessionLocal() as session:
        rows = session.query(CommandAuth).all()
        return [
            CommandAuthStatus(
                provider=row.provider,
                needs_auth=bool(row.needs_auth),
                auth_error=row.auth_error,
                last_checked_at=row.last_checked_at,
                last_authed_at=row.last_authed_at,
            )
            for row in rows
        ]
