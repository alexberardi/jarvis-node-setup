"""Pydantic model for jarvis_command.yaml / jarvis_package.yaml manifest files.

Used by the manifest generator CLI and the command store installer to
parse/validate command manifests. Supports single commands and
multi-component bundles.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION: int = 1

VALID_COMPONENT_TYPES: list[str] = [
    "command",
    "agent",
    "device_protocol",
    "device_manager",
    "prompt_provider",
    "routine",
]

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


class ManifestComponent(BaseModel):
    """A single component within a package bundle."""

    type: Literal["command", "agent", "device_protocol", "device_manager", "prompt_provider", "routine"]
    name: str
    path: str
    description: str = ""


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

    # Multi-component bundles (explicit or inferred from repo structure)
    components: list[ManifestComponent] = Field(default_factory=list)

    @property
    def is_bundle(self) -> bool:
        """True if this package has multiple components or non-command types."""
        return len(self.components) > 1 or (
            len(self.components) == 1
            and self.components[0].type != "command"
        )

    @property
    def package_type(self) -> str:
        """'bundle' if multi-component, 'command' otherwise."""
        return "bundle" if self.is_bundle else "command"


# Convention: directory name → component type
COMPONENT_DIR_TYPES: dict[str, str] = {
    "commands": "command",
    "agents": "agent",
    "device_families": "device_protocol",
    "device_managers": "device_manager",
    "prompt_providers": "prompt_provider",
    "routines": "routine",
}

# Convention: component type → expected entry point filename
COMPONENT_ENTRY_POINTS: dict[str, str] = {
    "command": "command.py",
    "agent": "agent.py",
    "device_protocol": "protocol.py",
    "device_manager": "manager.py",
    "prompt_provider": "provider.py",
    "routine": "routine.json",
}


def infer_components(repo_dir: Any, manifest_name: str) -> list[ManifestComponent]:
    """Infer components from repo directory structure when not declared in manifest.

    Scans for:
    - command.py at root → single command
    - commands/<name>/command.py → command(s)
    - agents/<name>/agent.py → agent(s)
    - device_families/<name>/protocol.py → device protocol(s)
    - device_managers/<name>/manager.py → device manager(s)

    Args:
        repo_dir: Path to the cloned repo.
        manifest_name: Package name from manifest (used for root-level command.py).

    Returns:
        List of inferred ManifestComponents. Empty if nothing found.
    """
    from pathlib import Path
    repo_dir = Path(repo_dir)

    components: list[ManifestComponent] = []

    # Check for root-level command.py (simple single-command repo)
    if (repo_dir / "command.py").exists():
        components.append(ManifestComponent(
            type="command",
            name=manifest_name,
            path="command.py",
        ))

    # Scan convention directories
    for dir_name, comp_type in COMPONENT_DIR_TYPES.items():
        type_dir = repo_dir / dir_name
        if not type_dir.is_dir():
            continue

        entry_filename = COMPONENT_ENTRY_POINTS[comp_type]
        for sub_dir in sorted(type_dir.iterdir()):
            if not sub_dir.is_dir() or sub_dir.name.startswith(("_", ".")):
                continue
            entry_point = sub_dir / entry_filename
            if entry_point.exists():
                components.append(ManifestComponent(
                    type=comp_type,  # type: ignore[arg-type]
                    name=sub_dir.name,
                    path=str(entry_point.relative_to(repo_dir)),
                ))

    # Also check for root-level routine.json (simple single-routine repo)
    if (repo_dir / "routine.json").exists() and not any(c.type == "routine" for c in components):
        components.append(ManifestComponent(
            type="routine",
            name=manifest_name,
            path="routine.json",
        ))

    return components
