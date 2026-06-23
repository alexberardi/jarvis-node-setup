"""Service-discovery URL resolution tests — pins the LAN-vs-cloud behaviour
behind the kitchen-node "offline while alive" incident.

Root cause recap: the kitchen Pi is on the SAME LAN as command-center and the
MQTT broker (it pings the prod origin in ~2-3ms), yet its on-disk config pinned
the Cloudflare hostnames (``command-center.jarvisautomation.io`` /
``mqtt.jarvisautomation.io``). So BOTH the HTTP heartbeat and MQTT rode a
fragile WAN/Cloudflare leg; when that leg degraded the node was marked offline
and dropped messages, while SSH (LAN-direct) kept working.

These tests:
  1. Characterize the real resolution order (config-service > JSON fallback)
     and the JSON-config parsing for command-center + MQTT.
  2. Pin the ROOT CAUSE — JSON config CAN pin a node to cloud hostnames — so
     the regression is visible and any future "prefer-LAN" change is a
     deliberate edit to a known test.
  3. An xfail executable-spec for the deferred "prefer-LAN for LAN-resident
     nodes" fix, so the desired invariant is captured without breaking CI.
"""

import importlib

import pytest

import utils.service_discovery as sd


@pytest.fixture
def discovery(monkeypatch):
    """Service-discovery module with config-service disabled and a controllable
    JSON-config backend, restored after each test."""
    # Force the JSON-fallback path (config-service not initialized).
    monkeypatch.setattr(sd, "_initialized", False, raising=False)

    json_cfg: dict[str, str] = {}
    monkeypatch.setattr(sd, "_get_from_json_config", lambda key: json_cfg.get(key))

    # Clear MQTT env overrides so the JSON path is exercised deterministically.
    for var in ("JARVIS_MQTT_BROKER", "JARVIS_MQTT_PORT", "JARVIS_MQTT_SCHEME"):
        monkeypatch.delenv(var, raising=False)

    return sd, json_cfg


# ── Characterization: JSON-config resolution (the fallback every node uses) ──


def test_command_center_url_from_json_config(discovery):
    mod, cfg = discovery
    cfg["jarvis_command_center_api_url"] = "http://10.0.0.107:7703"
    assert mod.get_command_center_url() == "http://10.0.0.107:7703"


def test_mqtt_broker_url_built_from_json_parts(discovery):
    mod, cfg = discovery
    cfg["mqtt_broker"] = "10.0.0.107"
    cfg["mqtt_port"] = "1884"
    cfg["mqtt_scheme"] = "mqtt"
    assert mod.get_mqtt_broker_url() == "mqtt://10.0.0.107:1884"


def test_mqtt_broker_url_defaults_when_unconfigured(discovery):
    mod, _cfg = discovery
    # No env, no JSON → safe localhost default (never a cloud host).
    assert mod.get_mqtt_broker_url() == "mqtt://localhost:1884"


def test_mqtt_broker_url_env_overrides_json(monkeypatch, discovery):
    mod, cfg = discovery
    cfg["mqtt_broker"] = "10.0.0.107"  # JSON says LAN
    monkeypatch.setenv("JARVIS_MQTT_BROKER", "mqtt-host.example")
    monkeypatch.setenv("JARVIS_MQTT_PORT", "8883")
    monkeypatch.setenv("JARVIS_MQTT_SCHEME", "mqtts")
    # Env wins over JSON (resolution order in get_mqtt_broker_url).
    assert mod.get_mqtt_broker_url() == "mqtts://mqtt-host.example:8883"


# ── Root-cause pin: JSON config CAN route a LAN node over the cloud ──────────


def test_json_config_can_pin_node_to_cloud_hostnames(discovery):
    """Documents EXACTLY how the kitchen node ended up on the WAN path.

    Provisioning baked the Cloudflare hostnames into the node's JSON config,
    so resolution faithfully returns cloud URLs for a node that is physically
    on the LAN. This is the defect to eliminate at the provisioning source;
    until then, this is the (correct, faithful) behaviour of resolution.
    """
    mod, cfg = discovery
    cfg["jarvis_command_center_api_url"] = "https://command-center.jarvisautomation.io"
    cfg["mqtt_broker"] = "mqtt.jarvisautomation.io"
    cfg["mqtt_port"] = "443"
    cfg["mqtt_scheme"] = "wss"

    assert mod.get_command_center_url() == "https://command-center.jarvisautomation.io"
    assert mod.get_mqtt_broker_url() == "wss://mqtt.jarvisautomation.io:443"


# ── Deferred "prefer-LAN" invariant (executable spec, expected to fail) ──────


@pytest.mark.xfail(
    reason="prefer-LAN for LAN-resident nodes is deferred (node-side fix not "
    "yet implemented). When a node can reach a LAN origin, heartbeat + MQTT "
    "must NOT resolve to a Cloudflare host. See node-offline-mqtt root-cause.",
    strict=False,
)
def test_lan_resident_node_should_not_resolve_cloud_broker(monkeypatch, discovery):
    """DESIRED invariant: a node that advertises a LAN reachability hint must
    never hand back a wss://*.jarvisautomation.io broker URL, even if the JSON
    config still carries cloud hostnames. Today resolution has no LAN
    preference, so this xfails — flip to a real assertion when prefer-LAN lands.
    """
    mod, cfg = discovery
    # Node is on the LAN with the origin...
    monkeypatch.setenv("JARVIS_PREFER_LAN", "1")
    cfg["lan_origin_host"] = "10.0.0.107"
    # ...but its config still points at the cloud (provisioning artifact).
    cfg["mqtt_broker"] = "mqtt.jarvisautomation.io"
    cfg["mqtt_port"] = "443"
    cfg["mqtt_scheme"] = "wss"

    url = mod.get_mqtt_broker_url()

    assert not url.startswith("wss://"), url
    assert "jarvisautomation.io" not in url, url


def test_module_reimports_cleanly():
    """Guard: the module imports without side effects (no network at import)."""
    importlib.reload(sd)
