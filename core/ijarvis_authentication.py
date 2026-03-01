"""Declarative authentication configuration for IJarvisCommand.

Commands declare what auth they need via AuthenticationConfig.
The mobile app reads this config and acts as a generic OAuth executor.
The node handles post-processing (e.g., creating long-lived tokens).
"""

from dataclasses import dataclass, field


@dataclass
class AuthenticationConfig:
    """Declarative OAuth config for commands that need external auth.

    Two modes:
    - External (Spotify, Google): authorize_url + exchange_url are full URLs.
      No discovery needed.
    - Local (HA): authorize_path + exchange_path are relative paths.
      discovery_port triggers a mobile network scan. Mobile assembles
      full URLs from discovered IP + paths.
    """

    type: str                              # "oauth" (extensible to "api_key", "basic" later)
    provider: str                          # "home_assistant", "spotify" — groups commands sharing auth
    client_id: str                         # OAuth client ID
    keys: list[str]                        # Keys to extract from token response: ["access_token"]

    # For external OAuth (full URLs known):
    authorize_url: str | None = None       # "https://accounts.spotify.com/authorize"
    exchange_url: str | None = None        # "https://accounts.spotify.com/api/token"

    # For local/discoverable OAuth (HA — relative paths + discovery):
    authorize_path: str | None = None      # "/auth/authorize"
    exchange_path: str | None = None       # "/auth/token"
    discovery_port: int | None = None      # 8123
    discovery_probe_path: str | None = None  # "/api/"

    # OAuth extras:
    scopes: list[str] = field(default_factory=list)
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    extra_exchange_params: dict[str, str] = field(default_factory=dict)
    send_redirect_uri_in_exchange: bool = True
    supports_pkce: bool = False  # HA doesn't support PKCE, Spotify/Google do

    def to_dict(self) -> dict:
        """Serialize for settings snapshot (sent to mobile app)."""
        result: dict = {
            "type": self.type,
            "provider": self.provider,
            "client_id": self.client_id,
            "keys": self.keys,
        }

        if self.authorize_url:
            result["authorize_url"] = self.authorize_url
        if self.exchange_url:
            result["exchange_url"] = self.exchange_url
        if self.authorize_path:
            result["authorize_path"] = self.authorize_path
        if self.exchange_path:
            result["exchange_path"] = self.exchange_path
        if self.discovery_port is not None:
            result["discovery_port"] = self.discovery_port
        if self.discovery_probe_path:
            result["discovery_probe_path"] = self.discovery_probe_path
        if self.scopes:
            result["scopes"] = self.scopes
        if self.extra_authorize_params:
            result["extra_authorize_params"] = self.extra_authorize_params
        if self.extra_exchange_params:
            result["extra_exchange_params"] = self.extra_exchange_params
        if not self.send_redirect_uri_in_exchange:
            result["send_redirect_uri_in_exchange"] = False
        if self.supports_pkce:
            result["supports_pkce"] = True

        return result
