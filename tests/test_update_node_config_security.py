"""Security: update_node_config must not repoint the node's trust anchor.

`update_node_config` is a node-preference channel (volume, LEDs, mute, room, wake
settings). It used to `config.update(settings)` blindly, so an MQTT message could
overwrite `jarvis_command_center_api_url` — the key every verify-gate resolves the
CC from — or the node's identity / credentials / broker location. That repoints
the CC the node trusts → node RCE. These tests pin the trust-anchor denylist.
"""
import json
import sys
from unittest.mock import MagicMock

# Mock native/heavy deps before importing the module (sqlcipher3 is a C ext
# absent on dev; db.py runs create_engine() at import).
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = MagicMock()

from scripts.mqtt_tts_listener import (  # noqa: E402
    _reject_trust_anchor_keys,
    handle_update_node_config,
)


class TestRejectTrustAnchorKeys:
    def test_drops_the_command_center_url(self):
        safe = _reject_trust_anchor_keys(
            {"jarvis_command_center_api_url": "http://evil:7703", "room": "kitchen"}
        )
        assert safe == {"room": "kitchen"}

    def test_drops_every_service_url_key_by_pattern(self):
        forbidden = {
            "jarvis_command_center_api_url": "x",
            "jarvis_auth_api_url": "x",
            "jarvis_config_service_url": "x",
            "some_future_service_url": "x",
        }
        assert _reject_trust_anchor_keys(forbidden) == {}

    def test_drops_identity_and_broker_keys(self):
        forbidden = {
            "node_id": "x",
            "api_key": "x",
            "mqtt_broker": "x",
            "mqtt_port": 1883,
            "mqtt_username": "x",
            "mqtt_password": "x",
        }
        assert _reject_trust_anchor_keys(forbidden) == {}

    def test_keeps_legitimate_preference_keys(self):
        safe_input = {
            "volume_percent": 50,
            "led_enabled": True,
            "led_brightness_percent": 40,
            "is_muted": False,
            "room": "office",
            "wake_word_model": "hey_jarvis",
        }
        assert _reject_trust_anchor_keys(safe_input) == safe_input


class TestHandleUpdateNodeConfigPersistence:
    def test_attacker_cannot_repoint_cc_url_but_safe_key_applies(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"jarvis_command_center_api_url": "http://real-cc:7703", "room": "office"}))
        monkeypatch.setenv("CONFIG_PATH", str(cfg))

        handle_update_node_config({"settings": {
            "jarvis_command_center_api_url": "http://evil:7703",  # attack
            "room": "kitchen",                                    # legit preference
        }})

        saved = json.loads(cfg.read_text())
        assert saved["jarvis_command_center_api_url"] == "http://real-cc:7703"  # untouched
        assert saved["room"] == "kitchen"  # preference applied

    def test_all_forbidden_settings_write_nothing(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        original = {"jarvis_command_center_api_url": "http://real-cc:7703", "api_key": "realkey"}
        cfg.write_text(json.dumps(original))
        monkeypatch.setenv("CONFIG_PATH", str(cfg))

        handle_update_node_config({"settings": {
            "jarvis_command_center_api_url": "http://evil:7703",
            "api_key": "attackerkey",
            "mqtt_password": "attackerpw",
        }})

        assert json.loads(cfg.read_text()) == original  # nothing written


class TestPolicyKeysBlockedOnPlaintextChannel:
    """Update/egress policy gates (PR #40) must not be settable over this
    channel: it is plaintext MQTT ferried by CC, so CC (or any broker
    publisher) could otherwise flip a consent flag. They are writable only
    via the K2-encrypted config-push path (config_type "node_config")."""

    def test_drops_every_update_policy_key(self):
        assert _reject_trust_anchor_keys({
            "allow_updates": True,
            "wake_word_model_autodownload_enabled": True,
            "release_track": "dev",
        }) == {}

    def test_policy_keys_never_persist_but_safe_keys_do(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"room": "office"}))
        monkeypatch.setenv("CONFIG_PATH", str(cfg))

        handle_update_node_config({"settings": {
            "allow_updates": True,   # policy key — must be dropped
            "room": "kitchen",       # legit preference
        }})

        saved = json.loads(cfg.read_text())
        assert "allow_updates" not in saved
        assert saved["room"] == "kitchen"
