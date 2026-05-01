"""Tests for Jarvis package dependencies — manifest field, install guards,
namespace generation, metadata persistence, and uninstall protection."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from core.command_manifest import CommandManifest, ManifestComponent
from services.command_store_service import (
    COMPONENT_INSTALL_DIRS,
    PACKAGES_DIR,
    InstallError,
    RemoveError,
    _check_jarvis_dependencies,
    _check_no_dependents,
    _find_main_class_name_ast,
    _generate_package_namespace,
    _write_package_metadata,
    register_package_lib_paths,
)


# ── Manifest parsing ──────────────────────────────────────────────────────


class TestManifestJarvisDependencies:
    def test_parses_jarvis_dependencies(self):
        manifest = CommandManifest(
            name="nest_pro",
            description="Extended Nest",
            jarvis_dependencies=["nest"],
        )
        assert manifest.jarvis_dependencies == ["nest"]

    def test_defaults_to_empty_list(self):
        manifest = CommandManifest(name="basic", description="Basic command")
        assert manifest.jarvis_dependencies == []

    def test_multiple_dependencies(self):
        manifest = CommandManifest(
            name="mega",
            description="Depends on many",
            jarvis_dependencies=["nest", "govee", "kasa"],
        )
        assert manifest.jarvis_dependencies == ["nest", "govee", "kasa"]

    def test_round_trips_through_yaml(self, tmp_path):
        data = {
            "name": "nest_pro",
            "description": "Extended Nest",
            "jarvis_dependencies": ["nest"],
        }
        yaml_path = tmp_path / "jarvis_package.yaml"
        yaml_path.write_text(yaml.dump(data))

        loaded = yaml.safe_load(yaml_path.read_text())
        manifest = CommandManifest(**loaded)
        assert manifest.jarvis_dependencies == ["nest"]


# ── Dependency check ──────────────────────────────────────────────────────


class TestCheckJarvisDependencies:
    def test_raises_when_dependency_not_installed(self, tmp_path):
        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            with pytest.raises(InstallError, match="'nest' is not installed"):
                _check_jarvis_dependencies(["nest"])

    def test_passes_when_dependency_installed(self, tmp_path):
        (tmp_path / "nest.json").write_text("{}")
        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            _check_jarvis_dependencies(["nest"])  # should not raise

    def test_checks_all_dependencies(self, tmp_path):
        (tmp_path / "nest.json").write_text("{}")
        # govee.json missing
        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            with pytest.raises(InstallError, match="'govee' is not installed"):
                _check_jarvis_dependencies(["nest", "govee"])


# ── AST class name discovery ─────────────────────────────────────────────


class TestFindMainClassNameAst:
    def test_finds_command_class(self, tmp_path):
        src = tmp_path / "command.py"
        src.write_text(
            "from jarvis_command_sdk import IJarvisCommand\n"
            "\n"
            "class MyWeather(IJarvisCommand):\n"
            "    pass\n"
        )
        assert _find_main_class_name_ast(src, "IJarvisCommand") == "MyWeather"

    def test_finds_protocol_class(self, tmp_path):
        src = tmp_path / "protocol.py"
        src.write_text(
            "from jarvis_command_sdk import IJarvisDeviceProtocol\n"
            "\n"
            "class NestProtocol(IJarvisDeviceProtocol):\n"
            "    pass\n"
        )
        assert _find_main_class_name_ast(src, "IJarvisDeviceProtocol") == "NestProtocol"

    def test_finds_attribute_style_base(self, tmp_path):
        src = tmp_path / "command.py"
        src.write_text(
            "import jarvis_command_sdk\n"
            "\n"
            "class Foo(jarvis_command_sdk.IJarvisCommand):\n"
            "    pass\n"
        )
        assert _find_main_class_name_ast(src, "IJarvisCommand") == "Foo"

    def test_returns_none_when_no_match(self, tmp_path):
        src = tmp_path / "command.py"
        src.write_text("class Unrelated:\n    pass\n")
        assert _find_main_class_name_ast(src, "IJarvisCommand") is None

    def test_handles_syntax_error(self, tmp_path):
        src = tmp_path / "bad.py"
        src.write_text("def broken(:\n")
        assert _find_main_class_name_ast(src, "IJarvisCommand") is None

    def test_handles_missing_file(self, tmp_path):
        assert _find_main_class_name_ast(tmp_path / "nope.py", "IJarvisCommand") is None


# ── Namespace generation ──────────────────────────────────────────────────


class TestGeneratePackageNamespace:
    def _setup_installed_component(
        self,
        tmp_path: Path,
        comp_type: str,
        comp_name: str,
        entry_file: str,
        source_code: str,
    ) -> Path:
        """Create a fake installed component on disk."""
        install_rel = COMPONENT_INSTALL_DIRS[comp_type]
        comp_dir = tmp_path / install_rel / comp_name
        comp_dir.mkdir(parents=True)
        (comp_dir / entry_file).write_text(source_code)
        return comp_dir

    def test_generates_for_command(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        self._setup_installed_component(
            tmp_path, "command", "weather_pro", "command.py",
            "from jarvis_command_sdk import IJarvisCommand\n"
            "class WeatherPro(IJarvisCommand):\n    pass\n",
        )

        manifest = CommandManifest(
            name="weather_pro",
            description="Extended weather",
            components=[ManifestComponent(type="command", name="weather_pro", path="commands/weather_pro/command.py")],
        )

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", tmp_path):
            _generate_package_namespace("weather_pro", manifest)

        init_path = packages_dir / "weather_pro" / "__init__.py"
        assert init_path.exists()
        content = init_path.read_text()
        assert "from commands.custom_commands.weather_pro.command import WeatherPro" in content
        assert "WeatherPro" in content
        assert "__all__" in content

    def test_generates_for_protocol(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        self._setup_installed_component(
            tmp_path, "device_protocol", "nest", "protocol.py",
            "from jarvis_command_sdk import IJarvisDeviceProtocol\n"
            "class NestProtocol(IJarvisDeviceProtocol):\n    pass\n",
        )

        manifest = CommandManifest(
            name="nest",
            description="Nest integration",
            components=[ManifestComponent(type="device_protocol", name="nest", path="device_families/nest/protocol.py")],
        )

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", tmp_path):
            _generate_package_namespace("nest", manifest)

        init_path = packages_dir / "nest" / "__init__.py"
        assert init_path.exists()
        content = init_path.read_text()
        assert "from device_families.custom_families.nest.protocol import NestProtocol" in content
        assert "__all__" in content

    def test_skips_routines(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        manifest = CommandManifest(
            name="morning",
            description="Morning routine",
            components=[ManifestComponent(type="routine", name="morning", path="routines/morning/routine.json")],
        )

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", tmp_path):
            _generate_package_namespace("morning", manifest)

        # No __init__.py should be created for routine-only packages
        assert not (packages_dir / "morning" / "__init__.py").exists()

    def test_handles_multiple_components(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        self._setup_installed_component(
            tmp_path, "device_protocol", "govee", "protocol.py",
            "from jarvis_command_sdk import IJarvisDeviceProtocol\n"
            "class GoveeProtocol(IJarvisDeviceProtocol):\n    pass\n",
        )
        self._setup_installed_component(
            tmp_path, "device_manager", "govee", "manager.py",
            "from jarvis_command_sdk import IJarvisDeviceManager\n"
            "class GoveeManager(IJarvisDeviceManager):\n    pass\n",
        )

        manifest = CommandManifest(
            name="govee",
            description="Govee integration",
            components=[
                ManifestComponent(type="device_protocol", name="govee", path="device_families/govee/protocol.py"),
                ManifestComponent(type="device_manager", name="govee", path="device_managers/govee/manager.py"),
            ],
        )

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", tmp_path):
            _generate_package_namespace("govee", manifest)

        content = (packages_dir / "govee" / "__init__.py").read_text()
        assert "GoveeProtocol" in content
        assert "GoveeManager" in content

    def test_skips_when_entry_point_missing(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        manifest = CommandManifest(
            name="ghost",
            description="Missing component",
            components=[ManifestComponent(type="command", name="ghost", path="commands/ghost/command.py")],
        )

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", tmp_path):
            _generate_package_namespace("ghost", manifest)

        # No __init__.py because entry point doesn't exist
        assert not (packages_dir / "ghost" / "__init__.py").exists()


# ── Metadata persistence ─────────────────────────────────────────────────


class TestMetadataIncludesDependencies:
    def test_writes_jarvis_dependencies(self, tmp_path):
        manifest = CommandManifest(
            name="nest_pro",
            description="Extended Nest",
            version="1.0.0",
            jarvis_dependencies=["nest"],
            components=[ManifestComponent(type="device_protocol", name="nest_pro", path="device_families/nest_pro/protocol.py")],
        )

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            _write_package_metadata(manifest, "https://github.com/test/repo", {})

        meta_path = tmp_path / "nest_pro.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["jarvis_dependencies"] == ["nest"]

    def test_writes_empty_list_when_no_deps(self, tmp_path):
        manifest = CommandManifest(name="basic", description="No deps", version="1.0.0")

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            _write_package_metadata(manifest, "local:.", {})

        data = json.loads((tmp_path / "basic.json").read_text())
        assert data["jarvis_dependencies"] == []


# ── Uninstall guard ──────────────────────────────────────────────────────


class TestCheckNoDependents:
    def test_blocks_removal_when_dependent_exists(self, tmp_path):
        # "nest" is installed
        (tmp_path / "nest.json").write_text(json.dumps({
            "package_name": "nest",
            "jarvis_dependencies": [],
        }))
        # "nest_pro" depends on "nest"
        (tmp_path / "nest_pro.json").write_text(json.dumps({
            "package_name": "nest_pro",
            "jarvis_dependencies": ["nest"],
        }))

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            with pytest.raises(RemoveError, match="nest_pro"):
                _check_no_dependents("nest")

    def test_allows_removal_when_no_dependents(self, tmp_path):
        (tmp_path / "nest.json").write_text(json.dumps({
            "package_name": "nest",
            "jarvis_dependencies": [],
        }))

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            _check_no_dependents("nest")  # should not raise

    def test_allows_removal_when_packages_dir_missing(self, tmp_path):
        nonexistent = tmp_path / "nope"
        with patch("services.command_store_service.PACKAGES_DIR", nonexistent):
            _check_no_dependents("anything")  # should not raise

    def test_reports_multiple_dependents(self, tmp_path):
        (tmp_path / "nest.json").write_text(json.dumps({
            "package_name": "nest",
            "jarvis_dependencies": [],
        }))
        (tmp_path / "nest_pro.json").write_text(json.dumps({
            "package_name": "nest_pro",
            "jarvis_dependencies": ["nest"],
        }))
        (tmp_path / "nest_ultra.json").write_text(json.dumps({
            "package_name": "nest_ultra",
            "jarvis_dependencies": ["nest"],
        }))

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            with pytest.raises(RemoveError, match="nest_pro") as exc_info:
                _check_no_dependents("nest")
            assert "nest_ultra" in str(exc_info.value)


# ── Namespace cleanup on uninstall ────────────────────────────────────────


class TestNamespaceCleanupOnUninstall:
    def test_init_py_removed_on_uninstall(self, tmp_path):
        """Verify the __init__.py and package dir are cleaned up during remove."""
        packages_dir = tmp_path / "packages"
        pkg_dir = packages_dir / "nest"
        pkg_dir.mkdir(parents=True)

        # Simulate auto-generated namespace
        (pkg_dir / "__init__.py").write_text("# auto-generated\n")

        # Simulate package metadata
        meta = {
            "package_name": "nest",
            "package_type": "bundle",
            "components": [],
            "component_dirs": {},
            "jarvis_dependencies": [],
        }
        (packages_dir / "nest.json").write_text(json.dumps(meta))

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._cleanup_secrets_for_package"), \
             patch("services.command_store_service._check_no_dependents"), \
             patch("services.command_store_service._refresh_discovery_caches"):
            from services.command_store_service import remove
            remove("nest")

        assert not (pkg_dir / "__init__.py").exists()
        assert not pkg_dir.exists()  # dir should be removed since it's empty
        assert not (packages_dir / "nest.json").exists()


# ── register_package_lib_paths ────────────────────────────────────────────


class TestRegisterPackageLibPaths:
    def test_adds_packages_dir_to_syspath(self, tmp_path):
        # Create a minimal packages dir with a metadata file
        (tmp_path / "test_pkg.json").write_text("{}")

        original_path = sys.path[:]
        try:
            with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
                register_package_lib_paths()
            assert str(tmp_path) in sys.path
        finally:
            sys.path[:] = original_path

    def test_does_not_duplicate_path(self, tmp_path):
        (tmp_path / "test_pkg.json").write_text("{}")

        original_path = sys.path[:]
        try:
            with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
                register_package_lib_paths()
                register_package_lib_paths()  # call twice
            assert sys.path.count(str(tmp_path)) == 1
        finally:
            sys.path[:] = original_path

    def test_skips_when_dir_missing(self, tmp_path):
        nonexistent = tmp_path / "nope"
        original_path = sys.path[:]
        try:
            with patch("services.command_store_service.PACKAGES_DIR", nonexistent):
                register_package_lib_paths()
            assert str(nonexistent) not in sys.path
        finally:
            sys.path[:] = original_path
