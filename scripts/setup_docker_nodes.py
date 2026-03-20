#!/usr/bin/env python3
"""Post-start setup for dockerized nodes.

Run after `docker compose -f docker-compose.multi-node.yaml up -d`.
For each running jarvis-node-* container:
  1. Delete stale unencrypted DB (if K1 didn't exist at first boot)
  2. Generate K1 + K2 encryption keys
  3. Install all commands (DB migrations + seed secrets)
  4. Restart the container so MQTT listener picks up the fresh DB
  5. Print K2 import strings for the mobile app

Usage:
    python scripts/setup_docker_nodes.py
    python scripts/setup_docker_nodes.py --nodes node-kitchen node-office
"""

import argparse
import json
import subprocess
import sys
import time


def run_in_container(container: str, cmd: str, timeout: int = 30) -> tuple[int, str]:
    """Run a command inside a container, return (exit_code, output)."""
    result = subprocess.run(
        ["docker", "exec", container, "sh", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def get_running_nodes(filter_names: list[str] | None = None) -> list[str]:
    """Get running jarvis-node-* container names."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=jarvis-node-"],
        capture_output=True, text=True,
    )
    containers = [name.strip() for name in result.stdout.splitlines() if name.strip()]
    if filter_names:
        containers = [c for c in containers if any(f in c for f in filter_names)]
    return sorted(containers)


def setup_node(container: str) -> str | None:
    """Set up a single node container. Returns K2 import string or None on failure."""
    name = container.replace("jarvis-", "")

    # 1. Delete stale unencrypted DB
    run_in_container(container, "rm -f /app/jarvis_node.db")

    # 2. Generate K1 + K2
    rc, output = run_in_container(container, "python utils/generate_dev_k2.py --force", timeout=15)
    if rc != 0:
        print(f"   FAILED: K2 generation failed: {output[:100]}")
        return None

    # Extract import string (last non-empty line starting with eyJ)
    import_string = None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("eyJ"):
            import_string = line
            break

    # 3. Install commands
    rc, output = run_in_container(container, "python scripts/install_command.py --all", timeout=60)
    if rc != 0 and "secrets seeded" not in output:
        print(f"   WARN: command install may have issues: {output[-100:]}")

    # Extract secret count
    for line in output.splitlines():
        if "secrets seeded" in line:
            print(f"   {line.strip()}")
            break

    # 4. Restart container so MQTT picks up fresh DB
    subprocess.run(["docker", "restart", container], capture_output=True, timeout=15)

    return import_string


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-start setup for Docker nodes")
    parser.add_argument("--nodes", nargs="*", help="Specific node names (e.g., node-kitchen node-office)")
    args = parser.parse_args()

    containers = get_running_nodes(args.nodes)
    if not containers:
        print("No running jarvis-node-* containers found.")
        print("Start them first: docker compose -f docker-compose.multi-node.yaml up -d")
        return 1

    print(f"=== Setting up {len(containers)} Docker nodes ===\n")

    k2_imports: dict[str, str] = {}

    for container in containers:
        name = container.replace("jarvis-", "")
        print(f"  {name}...")
        import_string = setup_node(container)
        if import_string:
            k2_imports[name] = import_string
            print(f"   OK")
        else:
            print(f"   FAILED")

    # Wait for restarts
    print(f"\nWaiting for containers to restart...")
    time.sleep(10)

    # Health check
    print("\nHealth checks:")
    for container in containers:
        name = container.replace("jarvis-", "")
        rc, output = run_in_container(container, "python -c \"import urllib.request, json; r=urllib.request.urlopen('http://localhost:7771/health', timeout=3); print(json.loads(r.read())['status'])\"", timeout=10)
        status = output.strip() if rc == 0 else "unreachable"
        print(f"  {name}: {status}")

    # Print K2 import strings
    if k2_imports:
        print("\n=== K2 Import Strings (for mobile app Import Key screen) ===\n")
        for name, k2_str in sorted(k2_imports.items()):
            print(f"--- {name} ---")
            print(k2_str)
            print()

        print("To copy a specific key to clipboard:")
        print("  echo -n '<string>' | pbcopy")

    return 0


if __name__ == "__main__":
    sys.exit(main())
