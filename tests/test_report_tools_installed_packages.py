"""Tests for handle_report_tools' installed_packages payload.

Phase 3/4 of resilient installs: each installed-package entry carries
`previous_version` (drives mobile's "Revert to X" action) and `health`
("failed" when any of the package's components sits in a discovery
failed-modules registry — red dot on the package card).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# scripts/ isn't a package; add it to sys.path so we can import mqtt_tts_listener.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _run_report_tools(installed, cmd_failed, agent_failed):
    """Drive handle_report_tools with mocks; return the POSTed payload."""
    import mqtt_tts_listener as mtl

    discovery = MagicMock()
    discovery.get_all_commands.return_value = {}
    discovery.get_failed_modules.return_value = cmd_failed

    agent_discovery = MagicMock()
    agent_discovery.get_failed_modules.return_value = agent_failed

    cc_client = MagicMock()
    cc_client.get_date_context.return_value = {}

    posted: dict = {}

    def fake_post(url, data=None, timeout=None):
        posted["url"] = url
        posted["data"] = data
        return {"ok": True}

    with patch("utils.service_discovery.get_command_center_url",
               return_value="http://cc:7703"), \
         patch("clients.rest_client.RestClient.post", side_effect=fake_post), \
         patch("utils.command_discovery_service.get_command_discovery_service",
               return_value=discovery), \
         patch("utils.agent_discovery_service.get_agent_discovery_service",
               return_value=agent_discovery), \
         patch("clients.jarvis_command_center_client.JarvisCommandCenterClient",
               return_value=cc_client), \
         patch("services.command_store_service.list_installed",
               return_value=installed):
        mtl.handle_report_tools({"reply_request_id": "req-rt-1"})

    return posted


class TestInstalledPackagesPayload:
    def test_previous_version_and_health_fields(self):
        installed = [
            {
                "package_name": "email",
                "version": "1.0.2",
                "previous_version": "1.0.1",
                "components": [
                    {"type": "command", "name": "email"},
                    {"type": "agent", "name": "email_alerts"},
                ],
            },
            {
                "package_name": "weather",
                "version": "2.0.0",
                "components": [
                    {"type": "command", "name": "get_weather_meteo"},
                ],
            },
        ]

        posted = _run_report_tools(
            installed,
            cmd_failed={},
            agent_failed={"email_alerts": "ImportError: cannot import name 'JarvisInbox'"},
        )

        pkgs = {p["name"]: p for p in posted["data"]["installed_packages"]}
        assert pkgs["email"]["previous_version"] == "1.0.1"
        assert pkgs["email"]["health"] == "failed"
        assert pkgs["weather"]["health"] == "ok"
        assert "previous_version" not in pkgs["weather"]

    def test_command_failed_module_marks_health_failed(self):
        installed = [
            {
                "package_name": "shopping",
                "version": "1.0.0",
                "components": [{"type": "command", "name": "shopping_list"}],
            },
        ]

        posted = _run_report_tools(
            installed, cmd_failed={"shopping_list": "boom"}, agent_failed={},
        )

        entry = posted["data"]["installed_packages"][0]
        assert entry["health"] == "failed"
        assert "previous_version" not in entry

    def test_healthy_package_without_failures(self):
        installed = [
            {
                "package_name": "news",
                "version": "1.0.0",
                "components": [{"type": "command", "name": "get_news"}],
            },
        ]

        posted = _run_report_tools(installed, cmd_failed={}, agent_failed={})

        entry = posted["data"]["installed_packages"][0]
        assert entry == {"name": "news", "version": "1.0.0", "health": "ok"}

    def test_failed_registry_errors_do_not_drop_packages(self):
        """A discovery service whose registry accessor raises must not break
        the installed_packages payload."""
        import mqtt_tts_listener as mtl

        discovery = MagicMock()
        discovery.get_all_commands.return_value = {}
        discovery.get_failed_modules.side_effect = RuntimeError("nope")

        agent_discovery = MagicMock()
        agent_discovery.get_failed_modules.side_effect = RuntimeError("nope")

        cc_client = MagicMock()
        cc_client.get_date_context.return_value = {}

        posted: dict = {}

        def fake_post(url, data=None, timeout=None):
            posted["data"] = data
            return {"ok": True}

        installed = [
            {
                "package_name": "news",
                "version": "1.0.0",
                "components": [{"type": "command", "name": "get_news"}],
            },
        ]

        with patch("utils.service_discovery.get_command_center_url",
                   return_value="http://cc:7703"), \
             patch("clients.rest_client.RestClient.post", side_effect=fake_post), \
             patch("utils.command_discovery_service.get_command_discovery_service",
                   return_value=discovery), \
             patch("utils.agent_discovery_service.get_agent_discovery_service",
                   return_value=agent_discovery), \
             patch("clients.jarvis_command_center_client.JarvisCommandCenterClient",
                   return_value=cc_client), \
             patch("services.command_store_service.list_installed",
                   return_value=installed):
            mtl.handle_report_tools({"reply_request_id": "req-rt-2"})

        entry = posted["data"]["installed_packages"][0]
        assert entry["health"] == "ok"
