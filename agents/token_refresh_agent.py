"""TokenRefreshAgent — proactively refreshes OAuth tokens before they expire.

Runs every 5 minutes. Discovers all IJarvisCommands with
``requires_background_refresh`` enabled, checks each provider's
``TOKEN_EXPIRES_AT_<PROVIDER>`` secret, and calls ``cmd.refresh_token()``
when the token is within the refresh window.

Deduplicates by provider so commands sharing the same OAuth provider
(e.g. multiple Google commands) only trigger one refresh per cycle.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret

logger = JarvisLogger(service="jarvis-node")

CHECK_INTERVAL_SECONDS = 300  # 5 minutes


class TokenRefreshAgent(IJarvisAgent):
    """Background agent that refreshes OAuth tokens before expiry."""

    def __init__(self) -> None:
        self._last_results: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "token_refresh"

    @property
    def description(self) -> str:
        return "Refreshes OAuth tokens for commands that require background refresh"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=CHECK_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    def validate_secrets(self) -> List[str]:
        """No secrets of its own — checks commands dynamically."""
        return []

    async def run(self) -> None:
        """Check all OAuth-enabled commands and refresh tokens nearing expiry."""
        from utils.command_discovery_service import get_command_discovery_service

        commands = get_command_discovery_service().get_all_commands(include_disabled=True)

        # Deduplicate by provider — one refresh per provider per cycle
        seen_providers: set[str] = set()
        self._last_results = {}

        for cmd in commands.values():
            auth = cmd.authentication
            if not auth or not auth.requires_background_refresh:
                continue
            if auth.provider in seen_providers:
                continue
            seen_providers.add(auth.provider)

            try:
                if self._needs_refresh(auth.provider, auth.refresh_interval_seconds):
                    logger.info("Refreshing token", provider=auth.provider)
                    success = cmd.refresh_token()
                    self._last_results[auth.provider] = "ok" if success else "failed"
                else:
                    self._last_results[auth.provider] = "fresh"
            except Exception as e:
                logger.error(
                    "Token refresh error",
                    provider=auth.provider,
                    error=str(e),
                )
                self._last_results[auth.provider] = f"error: {e}"

    def _needs_refresh(self, provider: str, refresh_interval_seconds: int) -> bool:
        """Check if a provider's token expires within the refresh window."""
        from services.secret_service import get_secret_value

        expires_at_str = get_secret_value(
            f"TOKEN_EXPIRES_AT_{provider.upper()}", "integration"
        )

        if not expires_at_str:
            # No expiry recorded — assume token needs refresh
            return True

        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            seconds_remaining = (expires_at - now).total_seconds()

            return seconds_remaining < refresh_interval_seconds
        except (ValueError, TypeError) as e:
            logger.warning(
                "Invalid TOKEN_EXPIRES_AT", provider=provider, error=str(e)
            )
            return True

    def get_context_data(self) -> Dict[str, Any]:
        return {}
