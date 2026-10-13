"""Tests for utils.reexec_env — re-exec after provisioning with the startup env.

scripts.main seeds JARVIS_CONFIG_URL (and JARVIS_CONFIG_URL_STYLE) from
config.json only when they are unset. A factory reset keeps
jarvis_config_service_url, so the provisioning-mode process seeds the OLD
server's URL; provisioning then writes the new one to config.json and
re-execs. The re-exec used to inherit the seeded variables, so the node kept
resolving every service from the old server (jarvis-dev, 2026-10-08: 401s
from the previous server, no MQTT credentials, broker rc=5, "0 tools").

Pins:
  - the snapshot is the environment as it was when taken, not a live view
  - variables set after the snapshot are gone from the re-exec env
  - variables present at startup (systemd Environment=, a real override)
    survive with their startup value, even if changed since
"""

import os

from utils import reexec_env


def test_snapshot_is_a_copy(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SNAP", "before")
    snap = reexec_env.snapshot()
    monkeypatch.setenv("JARVIS_TEST_SNAP", "after")
    assert snap["JARVIS_TEST_SNAP"] == "before"


def test_seeded_config_url_is_dropped(monkeypatch):
    monkeypatch.delenv("JARVIS_CONFIG_URL", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_URL_STYLE", raising=False)
    reexec_env.remember_startup_env()
    # What main does from the pre-provisioning config.json:
    monkeypatch.setenv("JARVIS_CONFIG_URL", "http://10.0.0.103:7700")
    monkeypatch.setenv("JARVIS_CONFIG_URL_STYLE", "external")

    env = reexec_env.env_for_reexec()

    assert "JARVIS_CONFIG_URL" not in env
    assert "JARVIS_CONFIG_URL_STYLE" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_startup_override_survives(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_URL", "http://override:7700")
    reexec_env.remember_startup_env()
    monkeypatch.setenv("JARVIS_CONFIG_URL", "http://changed-later:7700")

    assert reexec_env.env_for_reexec()["JARVIS_CONFIG_URL"] == "http://override:7700"


def test_without_a_snapshot_falls_back_to_current_env(monkeypatch):
    monkeypatch.setattr(reexec_env, "_startup_env", None)
    monkeypatch.setenv("JARVIS_TEST_NOSNAP", "x")
    assert reexec_env.env_for_reexec()["JARVIS_TEST_NOSNAP"] == "x"
