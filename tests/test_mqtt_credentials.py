"""Tests for MQTT-credential auto-fetch from command-center.

The node fetches the shared broker credential over its authenticated HTTP
channel and persists it to config.json, falling back to anonymous when broker
auth isn't enabled yet or command-center is unreachable (the transition window).
"""
import json
import sys
from unittest.mock import MagicMock, patch

# Mock native/heavy deps before importing project modules (mirrors the other
# node tests: sqlcipher3 is a C ext absent on dev, db.py runs create_engine()
# at import time).
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = MagicMock()

from utils.mqtt_credentials import fetch_and_persist_mqtt_credentials  # noqa: E402

CC = "utils.mqtt_credentials.get_command_center_url"
REST = "utils.mqtt_credentials.RestClient.get"


def test_fetches_and_persists_valid_credentials(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"node_id": "n1"}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg))

    with patch(CC, return_value="http://cc.local:7703"), \
         patch(REST, return_value={"username": "jarvis", "password": "pw123"}) as mget:
        user, pw = fetch_and_persist_mqtt_credentials()

    assert (user, pw) == ("jarvis", "pw123")
    # Hits the singular /node/ path (matches CC's router).
    assert mget.call_args[0][0] == "http://cc.local:7703/api/v0/node/mqtt-credentials"
    # Persisted to config.json under the keys get_mqtt_config reads, preserving
    # existing keys.
    saved = json.loads(cfg.read_text())
    assert saved["mqtt_username"] == "jarvis"
    assert saved["mqtt_password"] == "pw123"
    assert saved["node_id"] == "n1"


def test_returns_none_and_does_not_persist_when_broker_auth_disabled(tmp_path, monkeypatch):
    # CC returns nulls -> broker auth not enabled yet -> connect anonymously.
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"node_id": "n1"}))
    monkeypatch.setenv("CONFIG_PATH", str(cfg))

    with patch(CC, return_value="http://cc.local:7703"), \
         patch(REST, return_value={"username": None, "password": None}):
        user, pw = fetch_and_persist_mqtt_credentials()

    assert (user, pw) == (None, None)
    assert "mqtt_username" not in json.loads(cfg.read_text())


def test_returns_none_when_cc_unreachable():
    # RestClient.get returns None on any request failure.
    with patch(CC, return_value="http://cc.local:7703"), \
         patch(REST, return_value=None):
        assert fetch_and_persist_mqtt_credentials() == (None, None)


def test_returns_none_when_no_cc_url():
    with patch(CC, return_value=""):
        assert fetch_and_persist_mqtt_credentials() == (None, None)
