#!/usr/bin/env python3
"""Extract a built-in command into a store-ready GitHub repo structure.

Creates a directory with the required files for publishing to the command store:
- jarvis_command.yaml (generated from class introspection)
- command.py (copied from commands/)
- README.md (auto-generated)
- LICENSE (MIT default)

Usage:
    python scripts/extract_command.py <ClassName> --output <dir>

Example:
    python scripts/extract_command.py ControlDeviceCommand --output ../jarvis-command-control-device
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.generate_manifest import (  # noqa: E402
    generate_manifest,
    write_manifest,
    load_command_class,
    introspect_command,
)


def extract_command(
    class_name: str,
    output_dir: str,
    author_github: str = "jarvis-community",
) -> None:
    """Extract a built-in command to a store-ready directory.

    Args:
        class_name: The IJarvisCommand subclass name.
        output_dir: Directory to create the repo structure in.
        author_github: GitHub username for the manifest.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # Find the source file
    cls = load_command_class(class_name)
    source_file = sys.modules[cls.__module__].__file__
    if not source_file:
        print(f"Error: Cannot locate source file for {class_name}", file=sys.stderr)
        sys.exit(1)

    source_path = Path(source_file)
    instance = cls()

    # Generate manifest
    manifest = generate_manifest(
        class_name,
        path=str(source_path),
        output_dir=str(output),
        non_interactive=True,
    )
    manifest.author.github = author_github
    write_manifest(manifest, str(output))

    # Copy command.py
    shutil.copy2(source_path, output / "command.py")

    # Generate README
    readme = f"""# {manifest.display_name or manifest.name}

{manifest.description}

## Installation

```bash
# From the Jarvis Command Store
jarvis store install {manifest.name}

# Or directly from GitHub
jarvis store install --url https://github.com/{author_github}/jarvis-command-{manifest.name.replace('_', '-')}
```

## Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
"""
    for param in manifest.parameters:
        req = "Yes" if param.required else "No"
        desc = param.description or ""
        readme += f"| {param.name} | {param.param_type} | {req} | {desc} |\n"

    if manifest.secrets:
        readme += "\n## Configuration\n\n"
        readme += "| Secret | Description |\n|--------|-------------|\n"
        for secret in manifest.secrets:
            readme += f"| {secret.key} | {secret.description} |\n"

    readme += f"\n## License\n\n{manifest.license}\n"

    (output / "README.md").write_text(readme)

    # LICENSE
    (output / "LICENSE").write_text(
        "MIT License\n\n"
        "Permission is hereby granted, free of charge, to any person obtaining a copy "
        "of this software and associated documentation files (the \"Software\"), to deal "
        "in the Software without restriction, including without limitation the rights "
        "to use, copy, modify, merge, publish, distribute, sublicense, and/or sell "
        "copies of the Software, and to permit persons to whom the Software is "
        "furnished to do so, subject to the following conditions:\n\n"
        "The above copyright notice and this permission notice shall be included in all "
        "copies or substantial portions of the Software.\n\n"
        "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND.\n"
    )

    print(f"Extracted '{manifest.name}' to {output}")
    print(f"  Source: {source_path}")
    print(f"  Manifest: {output / 'jarvis_command.yaml'}")
    print(f"  README: {output / 'README.md'}")
    print(f"\nNext steps:")
    print(f"  1. Review and customize command.py (remove local imports)")
    print(f"  2. Update README.md")
    print(f"  3. Create a GitHub repo and push")
    print(f"  4. Submit to the store: POST /v1/commands")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a built-in command into a store-ready repo structure"
    )
    parser.add_argument("class_name", help="The IJarvisCommand subclass name")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--author", default="jarvis-community", help="GitHub author username")
    args = parser.parse_args()

    extract_command(args.class_name, args.output, args.author)


if __name__ == "__main__":
    main()
