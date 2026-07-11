"""Regression tests for provisioning credential persistence.

The bug these pin: re-provisioning a node registered a fresh identity with
command-center but a guard in ``_save_node_credentials`` refused to overwrite
the existing config, so the device stayed on its old — now deactivated —
credentials. Result: 401 on every call, MQTT rc=5, "offline". A real
provisioning handshake must overwrite; incidental callers must not.
"""

import json

import pytest

import provisioning.api as api


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setenv("CONFIG_PATH", str(p))
    return p


def test_refuses_overwrite_by_default(config_path):
    """An incidental/stray caller must NOT clobber a provisioned node."""
    config_path.write_text(json.dumps(
        {"node_id": "old-real-id", "api_key": "old-real-key", "room": "kitchen"}
    ))
    assert api._save_node_credentials("new-id", "new-key") is False
    saved = json.loads(config_path.read_text())
    assert saved["node_id"] == "old-real-id"
    assert saved["api_key"] == "old-real-key"


def test_reprovision_overwrites_existing_creds(config_path):
    """The provisioning handshake (allow_overwrite=True) replaces old creds and
    preserves the rest of the config — the actual fix."""
    config_path.write_text(json.dumps({
        "node_id": "old-real-id",
        "api_key": "old-real-key",
        "room": "basement",
        "jarvis_config_service_url": "http://10.0.0.71:7700",
    }))
    assert api._save_node_credentials("new-id", "new-key", allow_overwrite=True) is True
    saved = json.loads(config_path.read_text())
    assert saved["node_id"] == "new-id"
    assert saved["api_key"] == "new-key"
    # merge, not clobber — discovery bootstrap + room must survive
    assert saved["room"] == "basement"
    assert saved["jarvis_config_service_url"] == "http://10.0.0.71:7700"


def test_writes_over_placeholder_without_flag(config_path):
    """A fresh/placeholder config is adopted even without allow_overwrite."""
    config_path.write_text(json.dumps({"node_id": "your-node-id", "api_key": ""}))
    assert api._save_node_credentials("new-id", "new-key") is True
    assert json.loads(config_path.read_text())["node_id"] == "new-id"


def test_same_node_id_is_written(config_path):
    """Re-saving the same node's creds (e.g. key rotation) is allowed."""
    config_path.write_text(json.dumps({"node_id": "same-id", "api_key": "old-key"}))
    assert api._save_node_credentials("same-id", "rotated-key") is True
    assert json.loads(config_path.read_text())["api_key"] == "rotated-key"
