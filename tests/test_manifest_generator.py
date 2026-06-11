"""Tests for the manifest generator CLI."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from jarvis_command_sdk import IJarvisCommand, CommandExample
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import JarvisSecret
from core.ijarvis_package import JarvisPackage
from core.ijarvis_authentication import AuthenticationConfig
from core.command_response import CommandResponse
from core.command_manifest import (
    CommandManifest,
    ManifestSecret,
    ManifestParameter,
    ManifestPackage,
    ManifestAuthentication,
    ManifestAuthor,
    VALID_CATEGORIES,
)
from scripts.generate_manifest import (
    introspect_command,
    prompt_for_metadata,
    write_manifest,
    generate_manifest,
)


# ── Sample commands ────────────────────────────────────────────────────────


class SimpleCommand(IJarvisCommand):
    @property
    def command_name(self):
        return "get_stock_price"

    @property
    def description(self):
        return "Get real-time stock prices"

    @property
    def parameters(self):
        return [
            JarvisParameter("ticker", "string", required=True, description="Stock ticker symbol"),
            JarvisParameter("period", "string", required=False, description="Time period", enum_values=["1d", "1w", "1m"]),
        ]

    @property
    def required_secrets(self):
        return [
            JarvisSecret("FINNHUB_API_KEY", "Finnhub API key", "integration", "string"),
        ]

    @property
    def keywords(self):
        return ["stock", "price", "ticker"]

    @property
    def required_packages(self):
        return [JarvisPackage("finnhub-python", ">=2.0.0")]

    def generate_prompt_examples(self):
        return [CommandExample("check Apple stock", {"ticker": "AAPL"}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({})


class AuthCommand(IJarvisCommand):
    @property
    def command_name(self):
        return "control_device"

    @property
    def description(self):
        return "Control smart home devices"

    @property
    def parameters(self):
        return [JarvisParameter("entity_id", "string", required=True)]

    @property
    def required_secrets(self):
        return [
            JarvisSecret("HA_URL", "Home Assistant URL", "integration", "string", is_sensitive=False),
            JarvisSecret("HA_KEY", "API Key", "integration", "string"),
        ]

    @property
    def keywords(self):
        return ["device", "light", "switch"]

    @property
    def authentication(self):
        return AuthenticationConfig(
            type="oauth",
            provider="home_assistant",
            friendly_name="Home Assistant",
            client_id="jarvis",
            keys=["access_token"],
            authorize_path="/auth/authorize",
            exchange_path="/auth/token",
            discovery_port=8123,
            discovery_probe_path="/api/",
        )

    def generate_prompt_examples(self):
        return [CommandExample("turn on lights", {"entity_id": "light.living_room"})]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({})


# ── Tests ───────────────────────────────────────────────────────────────────


class TestIntrospectCommand:
    def test_simple_command(self):
        cmd = SimpleCommand()
        data = introspect_command(cmd)

        assert data["name"] == "get_stock_price"
        assert data["description"] == "Get real-time stock prices"
        assert data["keywords"] == ["stock", "price", "ticker"]
        assert data["platforms"] == []
        assert data["authentication"] is None

    def test_parameters_extracted(self):
        cmd = SimpleCommand()
        data = introspect_command(cmd)

        assert len(data["parameters"]) == 2
        ticker = data["parameters"][0]
        assert ticker.name == "ticker"
        assert ticker.param_type == "string"
        assert ticker.required is True

        period = data["parameters"][1]
        assert period.enum_values == ["1d", "1w", "1m"]
        assert period.required is False

    def test_secrets_extracted(self):
        cmd = SimpleCommand()
        data = introspect_command(cmd)

        assert len(data["secrets"]) == 1
        secret = data["secrets"][0]
        assert secret.key == "FINNHUB_API_KEY"
        assert secret.scope == "integration"
        assert secret.is_sensitive is True

    def test_packages_extracted(self):
        cmd = SimpleCommand()
        data = introspect_command(cmd)

        assert len(data["packages"]) == 1
        pkg = data["packages"][0]
        assert pkg.name == "finnhub-python"
        assert pkg.version == ">=2.0.0"

    def test_authentication_extracted(self):
        cmd = AuthCommand()
        data = introspect_command(cmd)

        auth = data["authentication"]
        assert auth is not None
        assert auth.type == "oauth"
        assert auth.provider == "home_assistant"
        assert auth.discovery_port == 8123
        assert auth.keys == ["access_token"]

    def test_no_authentication(self):
        cmd = SimpleCommand()
        data = introspect_command(cmd)
        assert data["authentication"] is None

    def test_sensitive_vs_non_sensitive_secrets(self):
        cmd = AuthCommand()
        data = introspect_command(cmd)

        secrets = {s.key: s for s in data["secrets"]}
        assert secrets["HA_URL"].is_sensitive is False
        assert secrets["HA_KEY"].is_sensitive is True


class TestPromptForMetadata:
    def test_non_interactive_defaults(self):
        meta = prompt_for_metadata("get_stock_price", non_interactive=True)

        assert meta["display_name"] == "Get Stock Price"
        assert meta["version"] == "0.1.0"
        assert meta["license"] == "MIT"
        # No more "0.9.0" scaffold placeholder — the field is omitted
        # entirely when there's no real floor to declare.
        assert "min_jarvis_version" not in meta
        assert isinstance(meta["author"], ManifestAuthor)

    def test_existing_placeholder_floor_not_carried_forward(self):
        existing = {"min_jarvis_version": "0.9.0"}
        meta = prompt_for_metadata("get_stock_price", existing=existing,
                                   non_interactive=True)
        assert "min_jarvis_version" not in meta

    def test_existing_real_floor_preserved(self):
        existing = {"min_jarvis_version": "0.1.100"}
        meta = prompt_for_metadata("get_stock_price", existing=existing,
                                   non_interactive=True)
        assert meta["min_jarvis_version"] == "0.1.100"

    def test_non_interactive_with_existing(self):
        existing = {
            "display_name": "Stock Checker",
            "author": {"github": "octocat"},
            "version": "1.2.0",
            "categories": ["finance"],
        }
        meta = prompt_for_metadata("get_stock_price", existing=existing, non_interactive=True)

        assert meta["display_name"] == "Stock Checker"
        assert meta["author"].github == "octocat"
        assert meta["version"] == "1.2.0"
        assert meta["categories"] == ["finance"]

    def test_interactive_with_input(self):
        inputs = iter([
            "My Stock Tool",   # display_name
            "myuser",          # github
            "2.0.0",           # version
            "1.0.0",           # min_jarvis_version
            "Apache-2.0",      # license
            "https://example.com",  # homepage
            "finance, information",  # categories
        ])

        with patch("builtins.input", side_effect=lambda prompt: next(inputs)):
            meta = prompt_for_metadata("get_stock_price")

        assert meta["display_name"] == "My Stock Tool"
        assert meta["author"].github == "myuser"
        assert meta["version"] == "2.0.0"
        assert meta["license"] == "Apache-2.0"
        assert meta["categories"] == ["finance", "information"]

    def test_interactive_accepts_defaults(self):
        inputs = iter([""] * 7)  # All enter = all defaults

        with patch("builtins.input", side_effect=lambda prompt: next(inputs)):
            meta = prompt_for_metadata("get_stock_price")

        assert meta["display_name"] == "Get Stock Price"
        assert meta["version"] == "0.1.0"

    def test_invalid_categories_filtered(self):
        inputs = iter([
            "",           # display_name
            "",           # github
            "",           # version
            "",           # min_jarvis_version
            "",           # license
            "",           # homepage
            "finance, bogus, weather",  # categories (bogus invalid)
        ])

        with patch("builtins.input", side_effect=lambda prompt: next(inputs)):
            meta = prompt_for_metadata("test")

        assert "finance" in meta["categories"]
        assert "weather" in meta["categories"]
        assert "bogus" not in meta["categories"]


class TestWriteManifest:
    def test_write_and_read_back(self):
        manifest = CommandManifest(
            name="get_stock_price",
            description="Get stock prices",
            keywords=["stock"],
            parameters=[ManifestParameter(name="ticker", param_type="string", required=True)],
            secrets=[ManifestSecret(key="API_KEY", scope="integration", value_type="string")],
            packages=[ManifestPackage(name="finnhub-python", version=">=2.0.0")],
            display_name="Stock Price",
            author=ManifestAuthor(github="octocat"),
            version="1.0.0",
            categories=["finance"],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_manifest(manifest, tmpdir)
            assert path.exists()

            with open(path) as f:
                data = yaml.safe_load(f)

            assert data["name"] == "get_stock_price"
            assert data["schema_version"] == 1
            assert data["author"]["github"] == "octocat"
            assert len(data["parameters"]) == 1
            assert data["parameters"][0]["name"] == "ticker"
            assert len(data["secrets"]) == 1
            assert data["packages"][0]["name"] == "finnhub-python"

    def test_null_authentication_preserved(self):
        manifest = CommandManifest(
            name="test",
            description="test",
            display_name="Test",
            author=ManifestAuthor(github="x"),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_manifest(manifest, tmpdir)
            with open(path) as f:
                data = yaml.safe_load(f)
            assert data["authentication"] is None

    def test_authentication_serialized(self):
        manifest = CommandManifest(
            name="test",
            description="test",
            display_name="Test",
            author=ManifestAuthor(github="x"),
            authentication=ManifestAuthentication(
                type="oauth",
                provider="ha",
                friendly_name="HA",
                client_id="c",
                keys=["access_token"],
                discovery_port=8123,
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_manifest(manifest, tmpdir)
            with open(path) as f:
                data = yaml.safe_load(f)
            assert data["authentication"]["provider"] == "ha"
            assert data["authentication"]["discovery_port"] == 8123


class TestGenerateManifest:
    def test_end_to_end_non_interactive(self):
        """Full generate from class to manifest object."""
        # Write the sample command to a temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd_path = Path(tmpdir) / "command.py"
            cmd_path.write_text(
                'from jarvis_command_sdk import IJarvisCommand, CommandExample\n'
                'from core.command_response import CommandResponse\n'
                'from core.ijarvis_parameter import JarvisParameter\n'
                '\n'
                'class TestCmd(IJarvisCommand):\n'
                '    @property\n'
                '    def command_name(self): return "test_gen"\n'
                '    @property\n'
                '    def description(self): return "Test generated"\n'
                '    @property\n'
                '    def parameters(self): return [JarvisParameter("q", "string", required=True)]\n'
                '    @property\n'
                '    def required_secrets(self): return []\n'
                '    @property\n'
                '    def keywords(self): return ["test"]\n'
                '    def generate_prompt_examples(self): return [CommandExample("test", {"q": "x"})]\n'
                '    def generate_adapter_examples(self): return self.generate_prompt_examples()\n'
                '    def run(self, request_info, **kwargs): return CommandResponse.success_response({})\n'
            )

            manifest = generate_manifest(
                "TestCmd",
                path=str(cmd_path),
                output_dir=tmpdir,
                non_interactive=True,
            )

            assert manifest.name == "test_gen"
            assert manifest.description == "Test generated"
            assert len(manifest.parameters) == 1
            assert manifest.parameters[0].name == "q"
            assert manifest.display_name == "Test Gen"
            assert manifest.version == "0.1.0"


class TestCommandManifest:
    def test_defaults(self):
        m = CommandManifest(name="x", description="y")
        assert m.schema_version == 1
        assert m.keywords == []
        assert m.platforms == []
        assert m.secrets == []
        assert m.packages == []
        assert m.parameters == []
        assert m.authentication is None
        assert m.version == "0.1.0"
        assert m.license == "MIT"

    def test_valid_categories_list(self):
        assert "finance" in VALID_CATEGORIES
        assert "smart-home" in VALID_CATEGORIES
        assert len(VALID_CATEGORIES) > 10


class TestCommandManifestAptPackages:
    """`apt_packages` is the bridge from manifest YAML into the install
    pipeline (`services/command_store_service._install_apt_deps`). Before #15
    Pydantic dropped this field silently — the wrapper was never invoked
    even when the manifest declared packages. Pin the contract."""

    def test_apt_packages_defaults_to_empty_list(self):
        m = CommandManifest(name="x", description="y")
        assert m.apt_packages == []

    def test_apt_packages_survives_validation(self):
        m = CommandManifest(name="x", description="y", apt_packages=["mpv", "ffmpeg"])
        assert m.apt_packages == ["mpv", "ffmpeg"]

    def test_apt_packages_round_trips_through_dict(self):
        """YAML-loaded manifests come in as **kwargs from a yaml.safe_load
        dict. The field must thread through cleanly so _install_apt_deps
        sees the values the author declared."""
        raw = {
            "name": "media_command",
            "description": "Plays music",
            "version": "1.0.0",
            "apt_packages": ["mpv", "alsa-utils"],
        }
        m = CommandManifest(**raw)
        assert m.apt_packages == ["mpv", "alsa-utils"]
        # `getattr` is how the install pipeline currently reads it — must
        # succeed even on instances built before the field existed.
        assert getattr(m, "apt_packages", None) == ["mpv", "alsa-utils"]

    def test_apt_packages_rejects_non_list(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            CommandManifest(name="x", description="y", apt_packages="mpv")  # type: ignore[arg-type]

    def test_apt_packages_rejects_non_string_entry(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            CommandManifest(name="x", description="y", apt_packages=["mpv", 42])  # type: ignore[list-item]


class TestCommandManifestPostInstall:
    """`post_install` carries declarative ops the node-side dispatcher
    runs after pip+apt. Pre-#19 Pydantic dropped the field silently; the
    install pipeline would call _run_post_install with an empty list and
    no service config would land. Same regression class as apt_packages."""

    def test_post_install_defaults_to_empty_list(self):
        m = CommandManifest(name="x", description="y")
        assert m.post_install == []

    def test_post_install_survives_validation_as_list_of_dicts(self):
        ops = [
            {"type": "configure_systemd_service", "service": "shairport-sync",
             "run_as": "jarvis_user", "group": "audio"},
            {"type": "set_config_file_value", "file": "/etc/shairport-sync.conf",
             "format": "libconfig", "section": "general",
             "key": "output_backend", "value": "pa"},
        ]
        m = CommandManifest(name="x", description="y", post_install=ops)
        assert len(m.post_install) == 2
        assert m.post_install[0]["type"] == "configure_systemd_service"
        assert m.post_install[1]["key"] == "output_backend"

    def test_post_install_round_trips_through_kwargs(self):
        """YAML-loaded manifests come in as **kwargs from yaml.safe_load."""
        raw = {
            "name": "media", "description": "media", "version": "1.0.0",
            "post_install": [
                {"type": "configure_systemd_service", "service": "foo"},
            ],
        }
        m = CommandManifest(**raw)
        assert m.post_install == [
            {"type": "configure_systemd_service", "service": "foo"},
        ]

    def test_post_install_rejects_non_list(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            CommandManifest(name="x", description="y", post_install="oops")  # type: ignore[arg-type]
