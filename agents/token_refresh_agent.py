"""TokenRefreshAgent — proactively refreshes OAuth tokens before they expire.

Runs every 5 minutes. Discovers all IJarvisCommands AND IJarvisDeviceProtocols
with ``requires_background_refresh`` enabled, checks each provider's
``TOKEN_EXPIRES_AT_<PROVIDER>`` secret, and performs a standard OAuth2
refresh_token grant when the token is within the refresh window.

Deduplicates by provider so components sharing the same OAuth provider
(e.g. multiple Google commands) only trigger one refresh per cycle.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx
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
        self._http: httpx.Client | None = None

    def _http_client(self) -> httpx.Client:
        """Reused HTTP client for the life of this singleton agent. A fresh
        httpx/urllib client per refresh leaks native memory (notably the
        per-call SSL context loaded into libcrypto) even when closed; on a
        background schedule that accumulates and degrades wake latency.
        Reuse one client instead.
        """
        if self._http is None or self._http.is_closed:
            self._http = httpx.Client(timeout=15)
        return self._http

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
                # User-scoped refresh tokens (e.g. per-user Google calendars)
                # exist once per configured user; integration-scoped ones
                # (e.g. the household Spotify) exist once total.
                if self._refresh_token_scope(auth, source) == "user":
                    self._refresh_provider_for_users(auth, source)
                    continue

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

    def _refresh_token_scope(self, auth: AuthenticationConfig, source: Any) -> str:
        """The declared scope of the provider's refresh-token secret.

        Read from the source's secret declarations (authoritative — user-scope
        rows are not seeded at install, so the DB can't answer this)."""
        for attr in ("all_possible_secrets", "required_secrets"):
            try:
                declared = getattr(source, attr, None) or []
            except Exception:
                declared = []
            for secret in declared:
                if getattr(secret, "key", None) == auth.refresh_token_secret_key:
                    return getattr(secret, "scope", "integration") or "integration"
        return "integration"

    def _refresh_provider_for_users(self, auth: AuthenticationConfig, source: Any) -> None:
        """Refresh a user-scoped provider once per user holding a refresh token.

        Tokens stored before user-scope threading existed live in a single
        integration-scope row — when no per-user rows exist yet, fall back to
        refreshing that legacy row so existing setups keep working."""
        from services.secret_service import get_all_secrets

        user_ids = sorted({
            row.user_id
            for row in get_all_secrets("user")
            if row.key == auth.refresh_token_secret_key
            and row.user_id is not None
            and row.value
        })
        if not user_ids:
            if self._needs_refresh(auth.provider, auth.refresh_interval_seconds):
                logger.info(
                    "Refreshing token (legacy integration row)", provider=auth.provider
                )
                ok = self._do_refresh(auth, source)
                self._last_results[auth.provider] = "legacy:ok" if ok else "legacy:failed"
            else:
                self._last_results[auth.provider] = "legacy:fresh"
            return

        results: list[str] = []
        for uid in user_ids:
            try:
                if self._needs_refresh(auth.provider, auth.refresh_interval_seconds, user_id=uid):
                    logger.info("Refreshing token", provider=auth.provider, user_id=uid)
                    ok = self._do_refresh(auth, source, user_id=uid)
                    results.append(f"u{uid}:ok" if ok else f"u{uid}:failed")
                else:
                    results.append(f"u{uid}:fresh")
            except Exception as e:
                logger.error(
                    "Token refresh error",
                    provider=auth.provider, user_id=uid, error=str(e),
                )
                results.append(f"u{uid}:error")
        self._last_results[auth.provider] = ", ".join(results)

    def _needs_refresh(
        self, provider: str, refresh_interval_seconds: int, user_id: int | None = None
    ) -> bool:
        """Check if a provider's token expires within the refresh window."""
        from services.secret_service import get_secret_value

        if user_id is not None:
            expires_at_str = get_secret_value(
                f"TOKEN_EXPIRES_AT_{provider.upper()}", "user", user_id=user_id
            )
        else:
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

    def _do_refresh(
        self, auth: AuthenticationConfig, source: Any, user_id: int | None = None
    ) -> bool:
        """Perform OAuth2 refresh_token grant and store new tokens.

        When ``user_id`` is given the refresh token is read from that user's
        row and the refreshed tokens are stored back under their identity
        (the SDK ContextVar is set around store_auth_values)."""
        if not auth.refresh_token_secret_key or not auth.exchange_url:
            logger.warning("Missing refresh config", provider=auth.provider)
            return False

        from services.secret_service import get_secret_value, set_secret

        if user_id is not None:
            refresh_tok = get_secret_value(
                auth.refresh_token_secret_key, "user", user_id=user_id
            )
        else:
            refresh_tok = get_secret_value(auth.refresh_token_secret_key, "integration")
        if not refresh_tok:
            logger.warning("No refresh token stored", provider=auth.provider, user_id=user_id)
            return False

        payload: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": auth.client_id,
        }
        if auth.client_secret:
            payload["client_secret"] = auth.client_secret

        try:
            resp = self._http_client().post(auth.exchange_url, data=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except Exception as e:
            logger.error("Refresh request failed", provider=auth.provider, error=str(e))
            return False

        access_token: str | None = data.get("access_token")
        if not access_token:
            logger.error("No access_token in refresh response", provider=auth.provider)
            return False

        # Store tokens via the source's store_auth_values. The ContextVar
        # carries the token owner so user-scoped writes resolve their rows
        # (no-op for integration-scoped sources).
        values: dict[str, str] = {"access_token": access_token}
        new_refresh: str | None = data.get("refresh_token")
        if new_refresh:
            values["refresh_token"] = new_refresh

        from jarvis_command_sdk import set_current_user_id

        set_current_user_id(user_id)
        try:
            source.store_auth_values(values)
        finally:
            set_current_user_id(None)

        # Update expiry timestamp
        expires_in = data.get("expires_in")
        if expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if user_id is not None:
                set_secret(
                    f"TOKEN_EXPIRES_AT_{auth.provider.upper()}",
                    expires_at.isoformat(),
                    "user",
                    user_id=user_id,
                )
            else:
                set_secret(
                    f"TOKEN_EXPIRES_AT_{auth.provider.upper()}",
                    expires_at.isoformat(),
                    "integration",
                )

        logger.info(
            "Token refreshed",
            provider=auth.provider, user_id=user_id, expires_in=expires_in,
        )
        return True

    def get_context_data(self) -> Dict[str, Any]:
        return {}
