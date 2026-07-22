"""Service-discovery URL resolution tests — pins the config-service-only,
fail-loud contract that replaced the old JSON-fallback resolution.

Background: nodes used to resolve service URLs through a fallback ladder
(config-service → JSON config keys → hardcoded ``localhost``). That ladder is
what let a node get pinned to a stale/cloud host (the kitchen-node "offline
while alive" incident) and silently collapse to ``mqtt://localhost:1884`` when
config-service was briefly unreachable (the "dark node after full-stack
restart" incident).

New contract (what these tests pin):
  1. **config-service is the single source of truth.** Every service —
     command-center AND the broker — resolves through the SAME ``_get_url``
     path. No per-service asymmetry.
  2. **No localhost / JSON fallback for service URLs.** ``config.json`` holds
     only the bootstrap address (``jarvis_config_service_url``); it is never a
     service-URL fallback.
  3. **Fail loud.** A provisioned node that can't resolve a service raises
     ``ServiceUnresolvedError`` rather than fabricating a bogus URL — the caller
     retries/surfaces. A node still in provisioning mode tolerates it ("").
"""

import importlib
import sys
import types

import pytest

import utils.service_discovery as sd
from utils.service_discovery import ServiceUnresolvedError


@pytest.fixture
def discovery(monkeypatch):
    """service_discovery wired to a controllable config-service backend.

    Injects a fake ``jarvis_config_client`` whose ``get_service_url`` reads a
    dict the test controls, marks discovery initialized, and defaults the node
    to *provisioned* (so unresolved services raise loudly)."""
    services: dict[str, str] = {}

    fake = types.ModuleType("jarvis_config_client")
    fake.get_service_url = lambda name: services.get(name)  # type: ignore[attr-defined]
    fake.init = lambda *a, **k: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jarvis_config_client", fake)

    monkeypatch.setattr(sd, "_initialized", True, raising=False)
    monkeypatch.setattr(sd, "_in_provisioning_mode", lambda: False)
    return sd, services


# ── config-service is the single source of truth ────────────────────────────


def test_command_center_resolves_from_config_service(discovery):
    mod, services = discovery
    services["command-center"] = "http://10.0.0.107:7703"
    assert mod.get_command_center_url() == "http://10.0.0.107:7703"


def test_broker_resolves_from_config_service(discovery):
    mod, services = discovery
    services["jarvis-mqtt-broker"] = "mqtt://10.0.0.107:1884"
    assert mod.get_mqtt_broker_url() == "mqtt://10.0.0.107:1884"


def test_broker_and_cc_use_the_same_resolver(discovery):
    """No CC-vs-broker asymmetry: both flow through _get_url → config-service,
    so they succeed and fail together instead of one going dark."""
    mod, services = discovery
    services["command-center"] = "http://server:7703"
    services["jarvis-mqtt-broker"] = "mqtt://server:1884"
    assert mod.get_command_center_url() == "http://server:7703"
    assert mod.get_mqtt_broker_url() == "mqtt://server:1884"


# ── LAN override (co-located nodes) ──────────────────────────────────────────


def test_lan_override_short_circuits_config_service(discovery, monkeypatch):
    """JARVIS_<SERVICE>_LAN_URL bypasses config-service entirely so a node on the
    same LAN as the server skips the cloud relay round-trip (~1.9s → ~7ms)."""
    mod, services = discovery
    services["command-center"] = "https://command-center.jarvisautomation.io:443"  # cloud
    monkeypatch.setenv("JARVIS_COMMAND_CENTER_LAN_URL", "http://10.0.0.107:7703")
    assert mod.get_command_center_url() == "http://10.0.0.107:7703"


def test_lan_override_unset_falls_through_to_config_service(discovery, monkeypatch):
    """Without the override, resolution is unchanged — remote/multi-household
    nodes leave it unset and keep using the cloud URLs from config-service."""
    mod, services = discovery
    monkeypatch.delenv("JARVIS_COMMAND_CENTER_LAN_URL", raising=False)
    services["command-center"] = "https://command-center.jarvisautomation.io:443"
    assert mod.get_command_center_url() == "https://command-center.jarvisautomation.io:443"


def test_lan_override_is_per_service(discovery, monkeypatch):
    """The override key derives from the service name — only the overridden
    service is redirected; others still resolve via config-service."""
    mod, services = discovery
    services["command-center"] = "https://cc.cloud:443"
    services["whisper"] = "https://whisper.cloud:443"
    monkeypatch.setenv("JARVIS_COMMAND_CENTER_LAN_URL", "http://10.0.0.107:7703")
    monkeypatch.delenv("JARVIS_WHISPER_LAN_URL", raising=False)
    assert mod.get_command_center_url() == "http://10.0.0.107:7703"   # overridden
    assert mod.get_whisper_url() == "https://whisper.cloud:443"        # not overridden


# ── No localhost/JSON fallback — fail loud ───────────────────────────────────


def test_unresolved_raises_loudly_when_provisioned(discovery):
    """A provisioned node with an unresolvable service raises instead of
    fabricating mqtt://localhost:1884 (the old dark-node trap)."""
    mod, _services = discovery  # nothing registered → nothing resolves
    with pytest.raises(ServiceUnresolvedError):
        mod.get_mqtt_broker_url()
    with pytest.raises(ServiceUnresolvedError):
        mod.get_command_center_url()


def test_service_url_is_never_taken_from_json_config(discovery, monkeypatch):
    """config.json is ONLY the bootstrap address. Even if a stale service URL
    sits in the JSON config, resolution must NOT use it — it raises instead.
    (This is exactly how nodes got pinned to stale/cloud hosts.)"""
    mod, _services = discovery  # config-service resolves nothing
    monkeypatch.setattr(mod, "_get_from_json_config",
                        lambda key: "http://stale-from-json:7703")
    with pytest.raises(ServiceUnresolvedError):
        mod.get_command_center_url()


# ── Provisioning-mode tolerance ──────────────────────────────────────────────


def test_unresolved_returns_empty_during_provisioning(discovery, monkeypatch):
    """A not-yet-provisioned node isn't expected to reach config-service, so an
    unresolved service returns '' rather than raising."""
    mod, _services = discovery
    monkeypatch.setattr(mod, "_in_provisioning_mode", lambda: True)
    assert mod.get_mqtt_broker_url() == ""
    assert mod.get_command_center_url() == ""


def test_provisioning_mode_honors_skip_env(monkeypatch):
    """JARVIS_SKIP_PROVISIONING_CHECK forces provisioning-mode tolerance."""
    monkeypatch.setenv("JARVIS_SKIP_PROVISIONING_CHECK", "true")
    assert sd._in_provisioning_mode() is True


def test_module_reimports_cleanly():
    """Guard: the module imports without side effects (no network at import)."""
    importlib.reload(sd)
