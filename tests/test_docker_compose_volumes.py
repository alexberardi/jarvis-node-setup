"""Tests for docker-compose.yaml volume declarations.

Asserts that the named volumes required for Pantry component persistence
(per jarvis-roadmap#4) are declared on the jarvis-node service and at the
top level. Config-shape assertions only — does not invoke docker.
"""

from pathlib import Path

import pytest
import yaml

from services.command_store_service import COMPONENT_INSTALL_DIRS, PACKAGES_DIR, _PROJECT_DIR


COMPOSE_PATH = Path(__file__).resolve().parents[1] / "docker-compose.yaml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _mounts(compose: dict) -> list[tuple[str, str]]:
    raw = compose["services"]["jarvis-node"]["volumes"]
    return [tuple(entry.split(":")[:2]) for entry in raw]


def test_jarvis_node_service_keeps_existing_config_and_data_volumes(compose: dict) -> None:
    pairs = set(_mounts(compose))
    assert ("jarvis-node-config", "/config") in pairs
    assert ("jarvis-node-data", "/data") in pairs


def test_jarvis_node_has_five_custom_component_volume_mounts(compose: dict) -> None:
    pairs = set(_mounts(compose))
    expected = {
        ("jarvis-node-custom-commands", "/app/commands/custom_commands"),
        ("jarvis-node-custom-agents", "/app/agents/custom_agents"),
        ("jarvis-node-custom-families", "/app/device_families/custom_families"),
        ("jarvis-node-custom-managers", "/app/device_managers/custom_managers"),
        ("jarvis-node-custom-routines", "/app/routines/custom_routines"),
    }
    missing = expected - pairs
    assert not missing, f"missing component volume mounts: {missing}"


def test_jarvis_node_has_packages_metadata_volume(compose: dict) -> None:
    pairs = set(_mounts(compose))
    assert ("jarvis-node-packages", "/root/.jarvis/packages") in pairs


def test_top_level_volumes_block_declares_all_eight_named_volumes(compose: dict) -> None:
    declared = set(compose["volumes"].keys())
    expected = {
        "jarvis-node-config",
        "jarvis-node-data",
        "jarvis-node-custom-commands",
        "jarvis-node-custom-agents",
        "jarvis-node-custom-families",
        "jarvis-node-custom-managers",
        "jarvis-node-custom-routines",
        "jarvis-node-packages",
    }
    assert declared == expected


def test_component_mount_targets_match_command_store_service_install_dirs(compose: dict) -> None:
    targets = {target for _, target in _mounts(compose) if target.startswith("/app/")}
    expected = {f"/app/{rel}" for rel in COMPONENT_INSTALL_DIRS.values()}
    missing = expected - targets
    assert not missing, (
        f"COMPONENT_INSTALL_DIRS entries not mounted in compose: {missing}. "
        f"_PROJECT_DIR={_PROJECT_DIR} (container-side: /app)"
    )


def test_packages_mount_target_matches_register_package_lib_paths_root(compose: dict) -> None:
    pairs = dict(_mounts(compose))
    target = next(t for v, t in pairs.items() if v == "jarvis-node-packages")
    assert target == "/root/.jarvis/packages"
    expected_suffix = str(PACKAGES_DIR).split(str(Path.home()))[-1]
    assert target.endswith(expected_suffix), (
        f"compose target {target!r} should end with {expected_suffix!r} "
        f"(PACKAGES_DIR suffix from command_store_service)"
    )


def test_no_anonymous_or_bind_mounts_on_jarvis_node(compose: dict) -> None:
    raw = compose["services"]["jarvis-node"]["volumes"]
    for entry in raw:
        parts = entry.split(":")
        assert len(parts) >= 2, f"anonymous mount detected: {entry!r}"
        left = parts[0]
        assert "/" not in left and "." not in left, (
            f"bind mount detected (left side not a named volume): {entry!r}"
        )
