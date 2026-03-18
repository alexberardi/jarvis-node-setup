"""Pydantic model for jarvis_command.yaml manifest files.

Used by the manifest generator CLI and the command store installer to
parse/validate command manifests.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


SCHEMA_VERSION: int = 1

VALID_CATEGORIES: list[str] = [
    "automation",
    "calendar",
    "communication",
    "entertainment",
    "finance",
    "fitness",
    "food",
    "games",
    "health",
    "home",
    "information",
    "media",
    "music",
    "news",
    "productivity",
    "shopping",
    "smart-home",
    "sports",
    "travel",
    "utilities",
    "weather",
]


class ManifestAuthor(BaseModel):
    github: str


class ManifestSecret(BaseModel):
    key: str
    scope: str
    value_type: str
    required: bool = True
    description: str = ""
    is_sensitive: bool = True
    friendly_name: str | None = None


class ManifestPackage(BaseModel):
    name: str
    version: str | None = None


class ManifestParameter(BaseModel):
    name: str
    param_type: str
    description: str | None = None
    required: bool = False
    default_value: Any | None = None
    enum_values: list[str] | None = None


class ManifestAuthentication(BaseModel):
    type: str
    provider: str
    friendly_name: str
    client_id: str
    keys: list[str]
    authorize_url: str | None = None
    exchange_url: str | None = None
    authorize_path: str | None = None
    exchange_path: str | None = None
    discovery_port: int | None = None
    discovery_probe_path: str | None = None
    scopes: list[str] = Field(default_factory=list)
    supports_pkce: bool = False
    native_redirect_uri: str | None = None


class CommandManifest(BaseModel):
    """Full manifest model matching jarvis_command.yaml schema."""

    schema_version: int = SCHEMA_VERSION

    # From IJarvisCommand class (introspected)
    name: str
    description: str
    keywords: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    secrets: list[ManifestSecret] = Field(default_factory=list)
    packages: list[ManifestPackage] = Field(default_factory=list)
    parameters: list[ManifestParameter] = Field(default_factory=list)
    authentication: ManifestAuthentication | None = None

    # From interactive prompts (author-provided)
    display_name: str = ""
    author: ManifestAuthor = Field(default_factory=lambda: ManifestAuthor(github=""))
    version: str = "0.1.0"
    min_jarvis_version: str = "0.9.0"
    license: str = "MIT"
    categories: list[str] = Field(default_factory=list)
    homepage: str = ""
