#!/usr/bin/env python3
"""Generate a jarvis_command.yaml manifest from an IJarvisCommand class.

Introspects the command class for properties (name, description, parameters,
secrets, keywords, etc.) and prompts for author-provided metadata.

Usage:
    python scripts/generate_manifest.py <ClassName> [--path <command.py>] [--output <dir>] [--non-interactive]
"""

import argparse
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ijarvis_command import IJarvisCommand  # noqa: E402
from jarvis_command_sdk.command import IJarvisCommand as SDKIJarvisCommand  # noqa: E402
from core.command_manifest import (  # noqa: E402
    CommandManifest,
    ManifestAuthor,
    ManifestSecret,
    ManifestPackage,
    ManifestParameter,
    ManifestAuthentication,
    VALID_CATEGORIES,
)


def load_command_class(class_name: str, path: str | None = None) -> type[IJarvisCommand]:
    """Load an IJarvisCommand subclass by name.

    Args:
        class_name: The class name to find.
        path: Optional path to a .py file. If not provided, scans commands/.

    Returns:
        The command class (not an instance).
    """
    if path:
        spec = importlib.util.spec_from_file_location("user_command", path)
        if spec is None or spec.loader is None:
            raise FileNotFoundError(f"Cannot load module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        # Scan all commands/ modules
        import commands
        import pkgutil

        module = None
        for _, mod_name, _ in pkgutil.iter_modules(commands.__path__):
            candidate = importlib.import_module(f"commands.{mod_name}")
            if hasattr(candidate, class_name):
                module = candidate
                break

        if module is None:
            raise ValueError(f"Class '{class_name}' not found in commands/")

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"Class '{class_name}' not found in module")
    if not (isinstance(cls, type) and (issubclass(cls, IJarvisCommand) or issubclass(cls, SDKIJarvisCommand))):
        raise TypeError(f"'{class_name}' is not an IJarvisCommand subclass")
    return cls


def introspect_command(cmd: IJarvisCommand) -> dict[str, Any]:
    """Extract manifest fields from a command instance."""
    data: dict[str, Any] = {
        "name": cmd.command_name,
        "description": cmd.description,
        "keywords": list(cmd.keywords),
        "platforms": list(getattr(cmd, "supported_platforms", [])),
    }

    # Secrets
    data["secrets"] = [
        ManifestSecret(
            key=s.key,
            scope=s.scope,
            value_type=s.value_type,
            required=s.required,
            description=s.description,
            is_sensitive=s.is_sensitive,
            friendly_name=s.friendly_name,
        )
        for s in cmd.required_secrets
    ]

    # Packages
    data["packages"] = [
        ManifestPackage(name=p.name, version=p.version)
        for p in cmd.required_packages
    ]

    # Parameters
    data["parameters"] = [
        ManifestParameter(
            name=p.name,
            param_type=p.param_type,
            description=p.description,
            required=p.required,
            default_value=p.default_value,
            enum_values=p.enum_values,
        )
        for p in cmd.parameters
    ]

    # Authentication
    auth = cmd.authentication
    if auth:
        data["authentication"] = ManifestAuthentication(
            type=auth.type,
            provider=auth.provider,
            friendly_name=auth.friendly_name,
            client_id=auth.client_id,
            keys=auth.keys,
            authorize_url=auth.authorize_url,
            exchange_url=auth.exchange_url,
            authorize_path=auth.authorize_path,
            exchange_path=auth.exchange_path,
            discovery_port=auth.discovery_port,
            discovery_probe_path=auth.discovery_probe_path,
            scopes=auth.scopes,
            supports_pkce=auth.supports_pkce,
            native_redirect_uri=auth.native_redirect_uri,
        )
    else:
        data["authentication"] = None

    return data


def load_existing_manifest(output_dir: str) -> dict[str, Any] | None:
    """Load existing manifest for default values."""
    manifest_path = Path(output_dir) / "jarvis_command.yaml"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return yaml.safe_load(f)
    return None


def prompt_for_metadata(
    command_name: str,
    existing: dict[str, Any] | None = None,
    non_interactive: bool = False,
) -> dict[str, Any]:
    """Prompt the user for metadata that can't be introspected.

    Args:
        command_name: The command name (for defaults).
        existing: Previously saved manifest values (for defaults).
        non_interactive: If True, use defaults without prompting.

    Returns:
        Dict of author-provided metadata fields.
    """
    defaults: dict[str, Any] = {}
    if existing:
        defaults = {
            "display_name": existing.get("display_name", ""),
            "github": existing.get("author", {}).get("github", ""),
            "version": existing.get("version", "0.1.0"),
            "min_jarvis_version": existing.get("min_jarvis_version", "0.9.0"),
            "license": existing.get("license", "MIT"),
            "categories": existing.get("categories", []),
            "homepage": existing.get("homepage", ""),
        }

    # Generate sensible defaults
    default_display = defaults.get("display_name") or command_name.replace("_", " ").title()
    default_github = defaults.get("github") or ""
    default_version = defaults.get("version") or "0.1.0"
    default_min = defaults.get("min_jarvis_version") or "0.9.0"
    default_license = defaults.get("license") or "MIT"
    default_categories = defaults.get("categories") or []
    default_homepage = defaults.get("homepage") or ""

    if non_interactive:
        return {
            "display_name": default_display,
            "author": ManifestAuthor(github=default_github or "unknown"),
            "version": default_version,
            "min_jarvis_version": default_min,
            "license": default_license,
            "categories": default_categories,
            "homepage": default_homepage,
        }

    print("\n--- Author-provided metadata ---")
    print("(Press Enter to accept defaults shown in brackets)\n")

    display_name = input(f"  Display name [{default_display}]: ").strip() or default_display
    github = input(f"  GitHub username [{default_github}]: ").strip() or default_github
    version = input(f"  Version [{default_version}]: ").strip() or default_version
    min_jarvis = input(f"  Min Jarvis version [{default_min}]: ").strip() or default_min
    license_val = input(f"  License [{default_license}]: ").strip() or default_license
    homepage = input(f"  Homepage [{default_homepage}]: ").strip() or default_homepage

    # Categories
    print(f"\n  Available categories: {', '.join(VALID_CATEGORIES)}")
    cat_default = ", ".join(default_categories) if default_categories else ""
    cat_input = input(f"  Categories (comma-separated) [{cat_default}]: ").strip()
    if cat_input:
        categories = [c.strip() for c in cat_input.split(",") if c.strip()]
    else:
        categories = default_categories

    # Validate categories
    invalid = [c for c in categories if c not in VALID_CATEGORIES]
    if invalid:
        print(f"  Warning: invalid categories ignored: {invalid}")
        categories = [c for c in categories if c in VALID_CATEGORIES]

    return {
        "display_name": display_name,
        "author": ManifestAuthor(github=github or "unknown"),
        "version": version,
        "min_jarvis_version": min_jarvis,
        "license": license_val,
        "categories": categories,
        "homepage": homepage,
    }


def generate_manifest(
    class_name: str,
    path: str | None = None,
    output_dir: str = ".",
    non_interactive: bool = False,
) -> CommandManifest:
    """Generate a CommandManifest from a command class.

    Args:
        class_name: The IJarvisCommand subclass name.
        path: Optional path to the command .py file.
        output_dir: Directory to write jarvis_command.yaml.
        non_interactive: If True, skip interactive prompts.

    Returns:
        The generated CommandManifest.
    """
    cls = load_command_class(class_name, path)
    instance = cls()

    # Introspect the class
    introspected = introspect_command(instance)

    # Load existing manifest for defaults
    existing = load_existing_manifest(output_dir)

    # Get author metadata
    metadata = prompt_for_metadata(
        introspected["name"],
        existing=existing,
        non_interactive=non_interactive,
    )

    # Build manifest
    manifest = CommandManifest(
        **introspected,
        **metadata,
    )

    return manifest


def write_manifest(manifest: CommandManifest, output_dir: str = ".") -> Path:
    """Write manifest to jarvis_command.yaml."""
    output_path = Path(output_dir) / "jarvis_command.yaml"

    # Convert to dict, handling Pydantic models
    data = manifest.model_dump(mode="json", exclude_none=False)

    # Remove None authentication
    if data.get("authentication") is None:
        data["authentication"] = None

    with open(output_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate jarvis_command.yaml from an IJarvisCommand class"
    )
    parser.add_argument("class_name", help="The IJarvisCommand subclass name")
    parser.add_argument("--path", help="Path to the command .py file")
    parser.add_argument("--output", default=".", help="Output directory (default: current)")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive prompts, use defaults",
    )

    args = parser.parse_args()

    try:
        manifest = generate_manifest(
            args.class_name,
            path=args.path,
            output_dir=args.output,
            non_interactive=args.non_interactive,
        )
        output_path = write_manifest(manifest, args.output)
        print(f"\nManifest written to: {output_path}")
        print(f"  command: {manifest.name}")
        print(f"  version: {manifest.version}")
        print(f"  parameters: {len(manifest.parameters)}")
        print(f"  secrets: {len(manifest.secrets)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
