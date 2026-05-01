#!/usr/bin/env python3
"""Verify extracted Pantry packages pass the same checks as the container test harness.

Mimics the Pantry test_harness.py import/instantiation/property checks locally,
without Docker. Run against one repo or batch-verify all extracted repos.

Usage:
    # Single repo
    python scripts/verify_package.py /path/to/jarvis-device-simplisafe

    # All jarvis-device-* and jarvis-cmd-* repos under a parent dir
    python scripts/verify_package.py --all /path/to/jarvis/

    # Verbose output
    python scripts/verify_package.py -v /path/to/repo
"""

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# SDK base classes (lazy-loaded so the script fails gracefully if SDK missing)
# ---------------------------------------------------------------------------

_SDK_CLASSES: dict[str, type] = {}


def _get_sdk_class(comp_type: str) -> type | None:
    if not _SDK_CLASSES:
        try:
            from jarvis_command_sdk import (
                IJarvisCommand,
                IJarvisDeviceProtocol,
                IJarvisDeviceManager,
                IJarvisAgent,
            )
            _SDK_CLASSES.update({
                "command": IJarvisCommand,
                "agent": IJarvisAgent,
                "device_protocol": IJarvisDeviceProtocol,
                "device_manager": IJarvisDeviceManager,
            })
        except ImportError:
            print("ERROR: jarvis_command_sdk not importable. Install it first.")
            sys.exit(1)
    return _SDK_CLASSES.get(comp_type)


# ---------------------------------------------------------------------------
# Per-type property checks (mirrors test_harness.py)
# ---------------------------------------------------------------------------

def _check_command(instance: Any, record) -> None:
    _check_attr(instance, "command_name", str, record)
    _check_attr(instance, "description", str, record)
    _check_attr(instance, "parameters", list, record, prop_name="parameters_type")
    _check_attr(instance, "required_secrets", list, record, prop_name="required_secrets_type")
    try:
        kw = instance.keywords
        assert isinstance(kw, list) and all(isinstance(k, str) for k in kw)
        record("keywords_type", True)
    except Exception as e:
        record("keywords_type", False, str(e))
    try:
        examples = instance.generate_prompt_examples()
        assert isinstance(examples, list)
        record("prompt_examples", True)
    except Exception as e:
        record("prompt_examples", False, str(e))
    try:
        examples = instance.generate_adapter_examples()
        assert isinstance(examples, list)
        record("adapter_examples", True)
    except Exception as e:
        record("adapter_examples", False, str(e))


def _check_agent(instance: Any, record) -> None:
    _check_attr(instance, "name", str, record)
    _check_attr(instance, "description", str, record)
    try:
        _ = instance.schedule
        record("schedule", True)
    except Exception as e:
        record("schedule", False, str(e))
    _check_attr(instance, "required_secrets", list, record, prop_name="required_secrets_type")


def _check_protocol(instance: Any, record) -> None:
    _check_attr(instance, "protocol_name", str, record)
    _check_attr(instance, "supported_domains", list, record)


def _check_device_manager(instance: Any, record) -> None:
    _check_attr(instance, "name", str, record)
    _check_attr(instance, "friendly_name", str, record)
    _check_attr(instance, "description", str, record)


def _check_attr(instance, attr: str, expected_type: type, record,
                prop_name: str | None = None) -> None:
    label = prop_name or attr
    try:
        val = getattr(instance, attr)
        assert isinstance(val, expected_type) and (not isinstance(val, str) or len(val) > 0)
        record(label, True)
    except Exception as e:
        record(label, False, str(e))


_TYPE_CHECKERS = {
    "command": _check_command,
    "agent": _check_agent,
    "device_protocol": _check_protocol,
    "device_manager": _check_device_manager,
}


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------

def verify_component(repo_dir: Path, comp: dict, verbose: bool = False) -> list[dict]:
    """Verify a single component. Returns list of test result dicts."""
    results: list[dict] = []
    comp_type = comp["type"]
    comp_name = comp["name"]
    comp_path = comp["path"]

    def record(name: str, passed: bool, error: str | None = None) -> None:
        results.append({"name": f"{comp_name}/{name}", "passed": passed, "error": error})

    if comp_type == "routine":
        # JSON validation only
        routine_file = repo_dir / comp_path
        try:
            import json
            with open(routine_file) as f:
                data = json.load(f)
            assert "trigger_phrases" in data and "steps" in data
            record("json_valid", True)
        except Exception as e:
            record("json_valid", False, str(e))
        return results

    base_class = _get_sdk_class(comp_type)
    if base_class is None:
        record("import", False, f"Unknown component type: {comp_type}")
        return results

    # Add component's parent dir to sys.path (same as test_harness.py)
    comp_file = Path(comp_path)
    parent = repo_dir / comp_file.parent
    repo_str = str(repo_dir)
    parent_str = str(parent)

    saved_path = sys.path[:]
    if parent_str not in sys.path:
        sys.path.insert(0, parent_str)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    mod_name = comp_file.stem
    # Clear any cached module so we get a fresh import
    for key in list(sys.modules.keys()):
        if key == mod_name or key.startswith(f"{mod_name}."):
            del sys.modules[key]

    try:
        mod = importlib.import_module(mod_name)
        record("import", True)
    except Exception as e:
        record("import", False, f"Import failed: {e}")
        sys.path[:] = saved_path
        return results

    # Find subclass
    cls = None
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, base_class) and obj is not base_class:
            cls = obj
            break

    if cls is None:
        record("find_class", False, f"No {base_class.__name__} subclass found")
        sys.path[:] = saved_path
        return results
    record("find_class", True)

    # Instantiate
    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        sys.path[:] = saved_path
        return results

    # Type-specific property checks
    checker = _TYPE_CHECKERS.get(comp_type)
    if checker:
        checker(instance, record)

    sys.path[:] = saved_path
    return results


def verify_repo(repo_dir: Path, verbose: bool = False) -> dict:
    """Verify all components in a package repo. Returns summary dict."""
    repo_dir = repo_dir.resolve()
    manifest_path = repo_dir / "jarvis_package.yaml"
    if not manifest_path.exists():
        return {"repo": repo_dir.name, "error": "No jarvis_package.yaml found", "passed": 0, "failed": 1}

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    components = manifest.get("components", [])
    if not components:
        components = _infer_components(repo_dir)

    if not components:
        return {"repo": repo_dir.name, "error": "No components found", "passed": 0, "failed": 1}

    all_results: list[dict] = []
    for comp in components:
        all_results.extend(verify_component(repo_dir, comp, verbose))

    passed = sum(1 for r in all_results if r["passed"])
    failed = sum(1 for r in all_results if not r["passed"])

    return {
        "repo": repo_dir.name,
        "components": len(components),
        "passed": passed,
        "failed": failed,
        "tests": all_results,
    }


def _infer_components(repo_dir: Path) -> list[dict]:
    """Infer components from directory structure (same as test_harness.py)."""
    dir_types = {
        "commands": ("command", "command.py"),
        "agents": ("agent", "agent.py"),
        "device_families": ("device_protocol", "protocol.py"),
        "device_managers": ("device_manager", "manager.py"),
    }
    components: list[dict] = []

    if (repo_dir / "command.py").exists():
        components.append({"type": "command", "name": "command", "path": "command.py"})

    for dir_name, (comp_type, entry_filename) in dir_types.items():
        type_dir = repo_dir / dir_name
        if not type_dir.is_dir():
            continue
        for sub_dir in sorted(type_dir.iterdir()):
            if not sub_dir.is_dir() or sub_dir.name.startswith(("_", ".")):
                continue
            entry_point = sub_dir / entry_filename
            if entry_point.exists():
                components.append({
                    "type": comp_type,
                    "name": sub_dir.name,
                    "path": str(entry_point.relative_to(repo_dir)),
                })
    return components


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Pantry packages pass container test checks")
    parser.add_argument("path", help="Path to a package repo, or parent dir with --all")
    parser.add_argument("--all", action="store_true", help="Verify all jarvis-device-* and jarvis-cmd-* repos under path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all test results, not just failures")
    args = parser.parse_args()

    target = Path(args.path).resolve()

    if args.all:
        repos = sorted(
            p for p in target.iterdir()
            if p.is_dir() and (p.name.startswith("jarvis-device-") or p.name.startswith("jarvis-cmd-"))
            and (p / "jarvis_package.yaml").exists()
        )
    else:
        repos = [target]

    total_passed = 0
    total_failed = 0
    failures: list[str] = []

    for repo in repos:
        result = verify_repo(repo, args.verbose)

        if "error" in result:
            print(f"FAIL  {result['repo']}: {result['error']}")
            total_failed += 1
            failures.append(result["repo"])
            continue

        total_passed += result["passed"]
        total_failed += result["failed"]

        if result["failed"] > 0:
            failed_tests = [t for t in result["tests"] if not t["passed"]]
            print(f"FAIL  {result['repo']} — {result['passed']}/{result['passed'] + result['failed']} tests passed")
            for t in failed_tests:
                print(f"      x {t['name']}: {t['error']}")
            failures.append(result["repo"])
        else:
            print(f"PASS  {result['repo']} — {result['passed']}/{result['passed']} tests passed")

        if args.verbose and result["failed"] == 0:
            for t in result["tests"]:
                print(f"      + {t['name']}")

    print()
    print(f"{'=' * 60}")
    print(f"Total: {total_passed} passed, {total_failed} failed across {len(repos)} repos")
    if failures:
        print(f"Failed: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All packages verified.")


if __name__ == "__main__":
    main()
