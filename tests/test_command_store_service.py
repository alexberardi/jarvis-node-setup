"""Tests for command_store_service — install, remove, list operations."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from core.command_manifest import CommandManifest, ManifestAuthor, ManifestParameter, ManifestSecret
from services.command_store_service import (
    install_from_github,
    remove,
    list_installed,
    get_installed_metadata,
    _validate_repo_structure,
    _check_platform_compatibility,
    _write_store_metadata,
    InstallError,
    RemoveError,
    CUSTOM_COMMANDS_DIR,
    STORE_METADATA_FILE,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _create_fake_repo(tmpdir: Path, manifest_overrides: dict | None = None) -> Path:
    """Create a fake command repo structure in a temp directory."""
    repo_dir = tmpdir / "repo"
    repo_dir.mkdir(parents=True)

    # Default manifest
    manifest = {
        "schema_version": 1,
        "name": "test_cmd",
        "description": "A test command",
        "keywords": ["test"],
        "platforms": [],
        "secrets": [],
        "packages": [],
        "parameters": [{"name": "q", "param_type": "string", "required": True}],
        "authentication": None,
        "display_name": "Test Command",
        "author": {"github": "testuser"},
        "version": "1.0.0",
        "min_jarvis_version": "0.9.0",
        "license": "MIT",
        "categories": ["utilities"],
        "homepage": "",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)

    with open(repo_dir / "jarvis_command.yaml", "w") as f:
        yaml.dump(manifest, f)

    (repo_dir / "command.py").write_text(
        'from jarvis_command_sdk import IJarvisCommand, CommandResponse, CommandExample, JarvisParameter\n'
        '\n'
        'class TestCmd(IJarvisCommand):\n'
        '    @property\n'
        '    def command_name(self): return "test_cmd"\n'
        '    @property\n'
        '    def description(self): return "Test"\n'
        '    @property\n'
        '    def parameters(self): return [JarvisParameter("q", "string", required=True)]\n'
        '    @property\n'
        '    def required_secrets(self): return []\n'
        '    @property\n'
        '    def keywords(self): return ["test"]\n'
        '    def generate_prompt_examples(self): return [CommandExample("test", {"q": "x"})]\n'
        '    def generate_adapter_examples(self): return self.generate_prompt_examples()\n'
        '    def run(self, ri, **kw): return CommandResponse.success_response({})\n'
    )

    (repo_dir / "README.md").write_text("# Test Command")
    (repo_dir / "LICENSE").write_text("MIT")

    return repo_dir


# ── Tests ───────────────────────────────────────────────────────────────────


class TestValidateRepoStructure:
    def test_valid_repo(self, tmp_path):
        repo = _create_fake_repo(tmp_path)
        manifest = _validate_repo_structure(repo)
        assert manifest.name == "test_cmd"
        assert manifest.version == "1.0.0"

    def test_missing_command_py(self, tmp_path):
        repo = _create_fake_repo(tmp_path)
        (repo / "command.py").unlink()
        with pytest.raises(InstallError, match="Missing required file: command.py"):
            _validate_repo_structure(repo)

    def test_missing_manifest(self, tmp_path):
        repo = _create_fake_repo(tmp_path)
        (repo / "jarvis_command.yaml").unlink()
        with pytest.raises(InstallError, match="Missing required file: jarvis_command.yaml"):
            _validate_repo_structure(repo)

    def test_invalid_manifest_schema(self, tmp_path):
        repo = _create_fake_repo(tmp_path)
        with open(repo / "jarvis_command.yaml", "w") as f:
            yaml.dump({"invalid_field_only": True}, f)
        with pytest.raises(InstallError, match="Invalid manifest"):
            _validate_repo_structure(repo)

    def test_empty_name(self, tmp_path):
        repo = _create_fake_repo(tmp_path, {"name": ""})
        with pytest.raises(InstallError, match="'name' is empty"):
            _validate_repo_structure(repo)


class TestCheckPlatformCompatibility:
    def test_no_platform_restriction(self):
        manifest = CommandManifest(name="x", description="x", platforms=[])
        _check_platform_compatibility(manifest)  # Should not raise

    def test_matching_platform(self):
        import platform as plat
        current = plat.system().lower()
        manifest = CommandManifest(name="x", description="x", platforms=[current])
        _check_platform_compatibility(manifest)  # Should not raise

    def test_mismatched_platform(self):
        manifest = CommandManifest(name="x", description="x", platforms=["nonexistent_os"])
        with pytest.raises(InstallError, match="requires platforms"):
            _check_platform_compatibility(manifest)


class TestInstallFromGithub:
    @patch("services.command_store_service._clone_repo")
    @patch("services.command_store_service._check_name_conflict")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._seed_secrets")
    def test_install_success(self, mock_seed, mock_pip, mock_conflict, mock_clone, tmp_path):
        # Use separate dirs so the finally cleanup doesn't delete custom_commands
        repo_parent = tmp_path / "clone"
        repo_parent.mkdir()
        repo = _create_fake_repo(repo_parent)
        mock_clone.return_value = repo

        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            manifest = install_from_github("https://github.com/test/repo", skip_tests=True)

        assert manifest.name == "test_cmd"
        assert (test_custom_dir / "test_cmd" / "command.py").exists()
        assert (test_custom_dir / "test_cmd" / "__init__.py").exists()
        assert (test_custom_dir / "test_cmd" / STORE_METADATA_FILE).exists()
        assert (test_custom_dir / "test_cmd" / "jarvis_command.yaml").exists()

    @patch("services.command_store_service._clone_repo")
    def test_install_missing_files_fails(self, mock_clone, tmp_path):
        repo = _create_fake_repo(tmp_path)
        (repo / "command.py").unlink()
        mock_clone.return_value = repo

        with pytest.raises(InstallError, match="Missing required file"):
            install_from_github("https://github.com/test/repo")

    @patch("services.command_store_service._clone_repo")
    @patch("services.command_store_service._check_name_conflict")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._seed_secrets")
    def test_install_replaces_existing(self, mock_seed, mock_pip, mock_conflict, mock_clone, tmp_path):
        repo_parent = tmp_path / "clone"
        repo_parent.mkdir()
        repo = _create_fake_repo(repo_parent)
        mock_clone.return_value = repo

        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        # Pre-create an existing installation
        existing_dir = test_custom_dir / "test_cmd"
        existing_dir.mkdir()
        (existing_dir / "old_file.txt").write_text("old")

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            install_from_github("https://github.com/test/repo", skip_tests=True)

        # Old file should be gone, new command.py should be there
        assert not (test_custom_dir / "test_cmd" / "old_file.txt").exists()
        assert (test_custom_dir / "test_cmd" / "command.py").exists()


class TestRemove:
    def test_remove_success(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        cmd_dir = test_custom_dir / "test_cmd"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.py").write_text("# test")
        (cmd_dir / STORE_METADATA_FILE).write_text("{}")

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            remove("test_cmd")

        assert not cmd_dir.exists()

    def test_remove_not_installed(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            with pytest.raises(RemoveError, match="not installed"):
                remove("nonexistent")


class TestListInstalled:
    def test_empty_list(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = list_installed()
        assert result == []

    def test_list_with_metadata(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        cmd_dir = test_custom_dir / "test_cmd"
        cmd_dir.mkdir(parents=True)

        metadata = {
            "command_name": "test_cmd",
            "version": "1.0.0",
            "repo_url": "https://github.com/test/repo",
            "installed_at": "2026-01-01T00:00:00Z",
        }
        with open(cmd_dir / STORE_METADATA_FILE, "w") as f:
            json.dump(metadata, f)

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = list_installed()

        assert len(result) == 1
        assert result[0]["command_name"] == "test_cmd"
        assert result[0]["version"] == "1.0.0"

    def test_list_without_metadata(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        cmd_dir = test_custom_dir / "manual_cmd"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.py").write_text("# manual")

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = list_installed()

        assert len(result) == 1
        assert result[0]["command_name"] == "manual_cmd"
        assert result[0]["version"] == "unknown"

    def test_list_skips_init_py(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()
        (test_custom_dir / "__init__.py").write_text("# init")

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = list_installed()
        assert result == []


class TestGetInstalledMetadata:
    def test_found(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        cmd_dir = test_custom_dir / "test_cmd"
        cmd_dir.mkdir(parents=True)

        metadata = {"command_name": "test_cmd", "version": "1.0.0"}
        with open(cmd_dir / STORE_METADATA_FILE, "w") as f:
            json.dump(metadata, f)

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = get_installed_metadata("test_cmd")

        assert result["version"] == "1.0.0"

    def test_not_found(self, tmp_path):
        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            result = get_installed_metadata("nonexistent")
        assert result is None


class TestWriteStoreMetadata:
    def test_writes_metadata(self, tmp_path):
        manifest = CommandManifest(
            name="test",
            description="Test",
            version="1.0.0",
            display_name="Test",
            author=ManifestAuthor(github="octocat"),
        )
        _write_store_metadata(tmp_path, manifest, "https://github.com/test/repo", danger_rating=2)

        meta_file = tmp_path / STORE_METADATA_FILE
        assert meta_file.exists()

        with open(meta_file) as f:
            data = json.load(f)

        assert data["command_name"] == "test"
        assert data["version"] == "1.0.0"
        assert data["danger_rating"] == 2
        assert data["author"] == "octocat"
        assert "installed_at" in data
