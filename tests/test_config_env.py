"""Pins the config-URL style selection shared by both node entrypoints.

Regression guard for the "Pi forced to `remote`" bug: a provisioned Pi pointed
at a real-IP config-service must pick `external` (so container-name HTTP rows
like command-center resolve to a reachable host), NOT `remote` (which leaves
them as host.docker.internal and unreachable off-box).
"""

from utils.config_env import config_url_style_for_host, config_url_style_for_url


# ── off-box (LAN Pi / other host) → external ─────────────────────────────────


def test_real_ip_host_is_external():
    assert config_url_style_for_host("10.0.0.71") == "external"


def test_real_hostname_is_external():
    assert config_url_style_for_host("jarvis-server.local") == "external"


def test_real_ip_url_is_external():
    # The exact shape a provisioned Pi carries in config.json.
    assert config_url_style_for_url("http://10.0.0.71:7700") == "external"


# ── Docker peer → dockerized ─────────────────────────────────────────────────


def test_docker_internal_is_dockerized():
    assert config_url_style_for_host("host.docker.internal") == "dockerized"
    assert config_url_style_for_url("http://host.docker.internal:7700") == "dockerized"


# ── same box → no style (default resolution) ─────────────────────────────────


def test_localhost_is_none():
    assert config_url_style_for_host("localhost") is None
    assert config_url_style_for_host("127.0.0.1") is None
    assert config_url_style_for_url("http://localhost:7700") is None


def test_empty_host_is_none():
    assert config_url_style_for_host("") is None
    assert config_url_style_for_url("") is None
