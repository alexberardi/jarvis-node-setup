"""TokenRefreshAgent — proactively refreshes OAuth tokens before they expire.

Runs every 5 minutes. Discovers all IJarvisCommands AND IJarvisDeviceProtocols
with ``requires_background_refresh`` enabled, checks each provider's
``TOKEN_EXPIRES_AT_<PROVIDER>`` secret, and performs a standard OAuth2
refresh_token grant when the token is within the refresh window.

Deduplicates by provider so components sharing the same OAuth provider
(e.g. multiple Google commands) only trigger one refresh per cycle.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_authentication import AuthenticationConfig
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
        """Check all OAuth-enabled commands and device protocols, refresh tokens nearing expiry."""
        # Collect auth sources from commands and device protocols
        auth_sources: list[tuple[AuthenticationConfig, Any]] = []

        # Commands
        from utils.command_discovery_service import get_command_discovery_service

        commands = get_command_discovery_service().get_all_commands(include_disabled=True)
        for cmd in commands.values():
            auth = cmd.authentication
            if auth and auth.requires_background_refresh:
                auth_sources.append((auth, cmd))

        # Device protocols
        try:
            from utils.device_family_discovery_service import get_device_family_discovery_service

            families = get_device_family_discovery_service().get_all_families()
            for protocol in families.values():
                auth = protocol.authentication
                if auth and auth.requires_background_refresh:
                    auth_sources.append((auth, protocol))
        except ImportError:
            pass

        # Deduplicate by provider — one refresh per provider per cycle
        seen_providers: set[str] = set()
        self._last_results = {}

        for auth, source in auth_sources:
            if auth.provider in seen_providers:
                continue
            seen_providers.add(auth.provider)

            try:
                if self._needs_refresh(auth.provider, auth.refresh_interval_seconds):
                    logger.info("Refreshing token", provider=auth.provider)
                    success = self._do_refresh(auth, source)
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

    def _do_refresh(self, auth: AuthenticationConfig, source: Any) -> bool:
        """Perform OAuth2 refresh_token grant and store new tokens."""
        if not auth.refresh_token_secret_key or not auth.exchange_url:
            logger.warning("Missing refresh config", provider=auth.provider)
            return False

        from services.secret_service import get_secret_value, set_secret

        refresh_tok = get_secret_value(auth.refresh_token_secret_key, "integration")
        if not refresh_tok:
            logger.warning("No refresh token stored", provider=auth.provider)
            return False

        payload: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": auth.client_id,
        }

        try:
            req = Request(
                auth.exchange_url,
                data=urlencode(payload).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urlopen(req, timeout=15) as resp:
                data: dict[str, Any] = json.loads(resp.read().decode())
        except Exception as e:
            logger.error("Refresh request failed", provider=auth.provider, error=str(e))
            return False

        access_token: str | None = data.get("access_token")
        if not access_token:
            logger.error("No access_token in refresh response", provider=auth.provider)
            return False

        # Store tokens via the source's store_auth_values
        values: dict[str, str] = {"access_token": access_token}
        new_refresh: str | None = data.get("refresh_token")
        if new_refresh:
            values["refresh_token"] = new_refresh
        source.store_auth_values(values)

        # Update expiry timestamp
        expires_in = data.get("expires_in")
        if expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            set_secret(
                f"TOKEN_EXPIRES_AT_{auth.provider.upper()}",
                expires_at.isoformat(),
                "integration",
            )

        logger.info("Token refreshed", provider=auth.provider, expires_in=expires_in)
        return True

    def get_context_data(self) -> Dict[str, Any]:
        return {}
