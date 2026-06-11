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
    install_from_local,
    remove,
    revert_package,
    has_previous_version,
    list_installed,
    get_installed_metadata,
    validate_package,
    _enforce_version_floors,
    _parse_semver,
    _run_preflight_validation,
    _validate_repo_structure,
    _check_platform_compatibility,
    _write_store_metadata,
    _install_apt_deps,
    _force_rmtree,
    _APT_MIN_FREE_BYTES,
    _APT_WRAPPER_PATH,
    _PREFLIGHT_VALIDATE_TIMEOUT_SECONDS,
    InstallError,
    RemoveError,
    RevertError,
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

    def test_remove_sweeps_orphans_missing_from_component_dirs(self, tmp_path):
        """Defensive sweep: dirs declared in ``components`` are removed even when
        ``component_dirs`` was written incomplete.

        Regression for the spotify_keepalive orphan-agent incident: an older
        install (or a partial metadata write) recorded only the command in
        ``component_dirs``, so the agent dir survived uninstall and kept the
        agent scheduler trying to run a missing module every 10 seconds.
        """
        # Project layout: command + agent dirs both populated.
        project_dir = tmp_path / "project"
        cmd_dir = project_dir / "commands" / "custom_commands" / "spotify"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.py").write_text("# spotify cmd")
        agent_dir = project_dir / "agents" / "custom_agents" / "spotify_keepalive"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.py").write_text("# keepalive agent")

        # Packages metadata: declares both components, but only records the
        # command in component_dirs — the bug being defended against.
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()
        pkg_meta = {
            "package_name": "spotify",
            "version": "1.0.0",
            "components": [
                {"type": "command", "name": "spotify", "path": "commands/spotify/command.py"},
                {"type": "agent", "name": "spotify_keepalive", "path": "agents/spotify_keepalive/agent.py"},
            ],
            "component_dirs": {
                "command:spotify": str(cmd_dir),
                # agent intentionally missing
            },
        }
        with open(packages_dir / "spotify.json", "w") as f:
            json.dump(pkg_meta, f)

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._PROJECT_DIR", project_dir):
            remove("spotify")

        assert not cmd_dir.exists(), "command dir should be removed via component_dirs"
        assert not agent_dir.exists(), "agent dir should be removed by the defensive sweep"
        assert not (packages_dir / "spotify.json").exists(), "package metadata should be removed"


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


class TestForceRmtree:
    def test_removes_user_owned_dir(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        (d / "file.txt").write_text("x")
        _force_rmtree(d)
        assert not d.exists()

    def test_missing_path_is_noop(self, tmp_path):
        # Should not raise.
        _force_rmtree(tmp_path / "does-not-exist")

    def test_falls_back_to_sudo_on_permission_error(self, tmp_path):
        d = tmp_path / "target"
        d.mkdir()
        (d / "file.txt").write_text("x")

        rmtree_calls: list = []
        original_rmtree = shutil.rmtree

        def fake_rmtree(path, *a, **kw):
            rmtree_calls.append(path)
            raise PermissionError(13, "Permission denied")

        run_calls: list = []

        def fake_run(cmd, **kw):
            run_calls.append(cmd)
            # Actually delete it so the test can assert success
            original_rmtree(d)
            return MagicMock(returncode=0)

        with patch("services.command_store_service.shutil.rmtree", side_effect=fake_rmtree), \
             patch("services.command_store_service.subprocess.run", side_effect=fake_run):
            _force_rmtree(d)

        assert len(rmtree_calls) == 1
        assert run_calls == [["sudo", "-n", "rm", "-rf", str(d)]]

    def test_raises_install_error_with_unblock_instructions_when_sudo_also_fails(self, tmp_path):
        import subprocess as _sub
        d = tmp_path / "target"
        d.mkdir()

        def fake_rmtree(path, *a, **kw):
            raise PermissionError(13, "Permission denied")

        def fake_run(cmd, **kw):
            raise _sub.CalledProcessError(
                returncode=1, cmd=cmd, stderr=b"sudo: a password is required\n"
            )

        with patch("services.command_store_service.shutil.rmtree", side_effect=fake_rmtree), \
             patch("services.command_store_service.subprocess.run", side_effect=fake_run):
            with pytest.raises(InstallError) as excinfo:
                _force_rmtree(d)

        msg = str(excinfo.value)
        assert str(d) in msg
        # The unblock command must be quotable into a shell verbatim.
        assert f"sudo rm -rf {d}" in msg


class TestInstallAptDeps:
    def _manifest(self, apt_packages=None, has_attr=True):
        m = CommandManifest(name="x", description="x")
        if has_attr:
            setattr(m, "apt_packages", apt_packages if apt_packages is not None else [])
        return m

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_installs_apt_packages_when_declared(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        manifest = self._manifest(["mpv", "alsa-utils"])

        _install_apt_deps(manifest)

        assert mock_run.call_count == 1
        argv = mock_run.call_args[0][0]
        assert argv[:4] == ["sudo", str(_APT_WRAPPER_PATH), "mpv", "alsa-utils"]

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_empty_apt_packages_is_a_noop(self, mock_disk, mock_run):
        manifest = self._manifest([])
        _install_apt_deps(manifest)
        mock_run.assert_not_called()
        mock_disk.assert_not_called()

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_missing_apt_packages_attr_is_a_noop(self, mock_disk, mock_run):
        manifest = self._manifest(has_attr=False)
        _install_apt_deps(manifest)
        mock_run.assert_not_called()
        mock_disk.assert_not_called()

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_disk_space_below_threshold_raises_install_error(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=400 * 1024 * 1024)
        manifest = self._manifest(["mpv"])
        with pytest.raises(InstallError, match=r"[Ii]nsufficient disk|need.*500|500\s*MB"):
            _install_apt_deps(manifest)
        mock_run.assert_not_called()

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_disk_space_at_exact_threshold_proceeds(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=_APT_MIN_FREE_BYTES)
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        manifest = self._manifest(["mpv"])
        _install_apt_deps(manifest)
        mock_run.assert_called_once()

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_wrapper_nonzero_exit_raises_install_error(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_run.return_value = MagicMock(
            returncode=4, stderr="invalid name: mpv\n", stdout=""
        )
        manifest = self._manifest(["mpv"])
        with pytest.raises(InstallError, match=r"apt.*invalid name: mpv|invalid name: mpv.*apt"):
            _install_apt_deps(manifest)

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_wrapper_missing_raises_clear_install_error(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_run.side_effect = FileNotFoundError("/usr/local/sbin/jarvis-apt-install")
        manifest = self._manifest(["mpv"])
        with pytest.raises(InstallError, match=r"install\.sh"):
            _install_apt_deps(manifest)
        # Hint at remediation
        try:
            _install_apt_deps(manifest)
        except InstallError as e:
            assert "re-run" in str(e).lower()

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_subprocess_timeout_raises_install_error(self, mock_disk, mock_run):
        import subprocess as _sp
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_run.side_effect = _sp.TimeoutExpired(cmd=["sudo"], timeout=300)
        manifest = self._manifest(["mpv"])
        with pytest.raises(InstallError, match=r"timeout|timed out"):
            _install_apt_deps(manifest)

    @patch("services.command_store_service.subprocess.run")
    @patch("services.command_store_service.shutil.disk_usage")
    def test_install_error_message_includes_failing_package_names(self, mock_disk, mock_run):
        mock_disk.return_value = MagicMock(free=10 * 1024**3)
        mock_run.return_value = MagicMock(
            returncode=4, stderr="invalid name: mpv\n", stdout=""
        )
        manifest = self._manifest(["mpv", "alsa-utils"])
        with pytest.raises(InstallError) as exc:
            _install_apt_deps(manifest)
        msg = str(exc.value)
        assert "invalid name: mpv" in msg
        assert "mpv" in msg and "alsa-utils" in msg


class TestParseSemver:
    def test_plain_semver(self):
        assert _parse_semver("1.2.3") == (1, 2, 3)

    def test_strips_leading_v(self):
        assert _parse_semver("v0.1.114") == (0, 1, 114)

    def test_pads_missing_parts(self):
        assert _parse_semver("1.2") == (1, 2, 0)
        assert _parse_semver("2") == (2, 0, 0)

    def test_extra_parts_ignored(self):
        assert _parse_semver("1.2.3.4") == (1, 2, 3)

    def test_garbage_parts_become_zero(self):
        assert _parse_semver("1.x.3") == (1, 0, 3)

    def test_garbage_string_collapses_to_zero(self):
        assert _parse_semver("not-a-version") == (0, 0, 0)
        assert _parse_semver("") == (0, 0, 0)
        assert _parse_semver("dev") == (0, 0, 0)

    def test_git_describe_suffixes_stripped(self):
        # A dirty checkout reports versions like these — the suffix must
        # not corrupt the patch part.
        assert _parse_semver("v0.1.114-dirty") == (0, 1, 114)
        assert _parse_semver("v0.1.97-12-gabc1234-dirty") == (0, 1, 97)

    def test_build_metadata_suffix_stripped(self):
        assert _parse_semver("1.2.3+build.7") == (1, 2, 3)

    def test_bare_sha_collapses_to_zero(self):
        # Unparseable ⇒ (0,0,0) ⇒ callers skip the floor check (skip-guard).
        assert _parse_semver("abc1234") == (0, 0, 0)


class TestManifestVersionFloorFields:
    def test_floors_default_to_none(self):
        manifest = CommandManifest(name="x", description="x")
        assert manifest.min_jarvis_version is None
        assert manifest.min_sdk_version is None

    def test_floors_parse_from_yaml(self):
        raw = yaml.safe_load(
            "name: x\n"
            "description: x\n"
            "min_jarvis_version: \"0.1.114\"\n"
            "min_sdk_version: \"0.3.3\"\n"
        )
        manifest = CommandManifest(**raw)
        assert manifest.min_jarvis_version == "0.1.114"
        assert manifest.min_sdk_version == "0.3.3"


class TestEnforceVersionFloors:
    def _manifest(self, **overrides) -> CommandManifest:
        return CommandManifest(name="floor_pkg", description="x", **overrides)

    @patch("services.command_store_service._node_version_str", return_value="0.1.100")
    def test_jarvis_floor_above_node_blocks(self, mock_node):
        manifest = self._manifest(min_jarvis_version="0.1.114")
        with pytest.raises(InstallError) as exc:
            _enforce_version_floors(manifest)
        msg = str(exc.value)
        assert "0.1.114" in msg and "0.1.100" in msg
        assert "Update the node first, then retry." in msg

    @patch("services.command_store_service._node_version_str", return_value="0.1.114")
    def test_jarvis_floor_equal_passes(self, mock_node):
        _enforce_version_floors(self._manifest(min_jarvis_version="0.1.114"))

    @patch("services.command_store_service._node_version_str", return_value="v0.2.0")
    def test_jarvis_floor_below_node_passes(self, mock_node):
        _enforce_version_floors(self._manifest(min_jarvis_version="0.1.50"))

    @patch("services.command_store_service._sdk_version_str", return_value="0.3.2")
    def test_sdk_floor_above_sdk_blocks(self, mock_sdk):
        manifest = self._manifest(min_sdk_version="0.3.3")
        with pytest.raises(InstallError) as exc:
            _enforce_version_floors(manifest)
        msg = str(exc.value)
        assert "SDK" in msg and "0.3.3" in msg and "0.3.2" in msg
        assert "Update the node first, then retry." in msg

    @patch("services.command_store_service._sdk_version_str", return_value="0.3.3")
    def test_sdk_floor_met_passes(self, mock_sdk):
        _enforce_version_floors(self._manifest(min_sdk_version="0.3.3"))

    def test_missing_floors_skip_checks(self):
        # No floors declared — must pass without even reading versions.
        with patch("services.command_store_service._node_version_str") as mock_node, \
             patch("services.command_store_service._sdk_version_str") as mock_sdk:
            mock_node.return_value = "0.1.1"
            mock_sdk.return_value = "0.1.1"
            _enforce_version_floors(self._manifest())

    @patch("services.command_store_service._node_version_str", return_value="0.1.100")
    def test_malformed_floor_skips_check(self, mock_node):
        # Bad metadata must not block installs.
        _enforce_version_floors(self._manifest(min_jarvis_version="banana"))

    @patch("services.command_store_service._node_version_str", return_value="0.1.100")
    def test_legacy_scaffold_placeholder_floor_skipped(self, mock_node):
        # ~28 published packages carry the scaffold default "0.9.0", which
        # predates node versioning and enforcement — it carries no signal,
        # and 0.1.x nodes would otherwise block every install.
        _enforce_version_floors(self._manifest(min_jarvis_version="0.9.0"))
        _enforce_version_floors(self._manifest(min_jarvis_version="v0.9.0"))
        _enforce_version_floors(self._manifest(min_jarvis_version=" 0.9.0 "))

    @patch("services.command_store_service._node_version_str", return_value="0.1.100")
    def test_real_floor_still_blocks_despite_placeholder_exemption(self, mock_node):
        with pytest.raises(InstallError, match="0.1.200"):
            _enforce_version_floors(self._manifest(min_jarvis_version="0.1.200"))

    @patch("services.command_store_service._node_version_str",
           return_value="v0.1.114-12-gabc1234-dirty")
    def test_git_describe_node_version_above_floor_passes(self, mock_node):
        # A dirty/dev checkout's git-describe version must compare by its
        # semver prefix, not collapse to (0,0,0) or block real floors.
        _enforce_version_floors(self._manifest(min_jarvis_version="0.1.100"))

    @patch("services.command_store_service._node_version_str", return_value="dev")
    def test_unparseable_node_version_skips_check(self, mock_node):
        _enforce_version_floors(self._manifest(min_jarvis_version="0.1.114"))

    @patch("services.command_store_service._sdk_version_str", return_value="")
    def test_unparseable_sdk_version_skips_check(self, mock_sdk):
        _enforce_version_floors(self._manifest(min_sdk_version="0.3.3"))

    @patch("services.command_store_service._node_version_str", return_value="0.1.100")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    def test_floor_blocks_before_any_disk_writes(
        self, mock_pip, mock_apt, mock_node, tmp_path
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": "9.9.9"})
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            with pytest.raises(InstallError, match="needs node version 9.9.9"):
                install_from_local(repo)

        assert list(project_dir.iterdir()) == [], "no component dirs may be written"
        assert list(packages_dir.iterdir()) == [], "no package metadata may be written"
        mock_apt.assert_not_called()
        mock_pip.assert_not_called()


class TestPreflightValidationGate:
    """The subprocess validate gate runs after pip and before any scatter."""

    def _sandbox(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()
        return project_dir, packages_dir

    @patch("services.command_store_service._enable_in_registry")
    @patch("services.command_store_service._seed_secrets")
    @patch("services.command_store_service._check_name_conflicts")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._run_preflight_validation")
    def test_pip_runs_before_validate_gate(
        self, mock_validate, mock_pip, mock_apt, mock_conflicts, mock_seed,
        mock_enable, tmp_path,
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": None})
        project_dir, packages_dir = self._sandbox(tmp_path)

        parent = MagicMock()
        parent.attach_mock(mock_apt, "_install_apt_deps")
        parent.attach_mock(mock_pip, "_install_pip_deps")
        parent.attach_mock(mock_validate, "_run_preflight_validation")

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            install_from_local(repo)

        names = [c[0] for c in parent.mock_calls if c[0] in (
            "_install_apt_deps", "_install_pip_deps", "_run_preflight_validation",
        )]
        assert names == [
            "_install_apt_deps", "_install_pip_deps", "_run_preflight_validation",
        ]

    @patch("services.command_store_service._enable_in_registry")
    @patch("services.command_store_service._seed_secrets")
    @patch("services.command_store_service._check_name_conflicts")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._install_component")
    @patch("services.command_store_service._run_preflight_validation")
    def test_validate_gate_runs_before_scatter(
        self, mock_validate, mock_component, mock_pip, mock_apt,
        mock_conflicts, mock_seed, mock_enable, tmp_path,
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": None})
        project_dir, packages_dir = self._sandbox(tmp_path)
        mock_component.return_value = project_dir / "commands" / "custom_commands" / "test_cmd"

        parent = MagicMock()
        parent.attach_mock(mock_validate, "_run_preflight_validation")
        parent.attach_mock(mock_component, "_install_component")

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            install_from_local(repo)

        names = [c[0] for c in parent.mock_calls if c[0] in (
            "_run_preflight_validation", "_install_component",
        )]
        assert names.index("_run_preflight_validation") < names.index("_install_component")

    @patch("services.command_store_service._enable_in_registry")
    @patch("services.command_store_service._seed_secrets")
    @patch("services.command_store_service._check_name_conflicts")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch(
        "services.command_store_service._run_preflight_validation",
        side_effect=InstallError("Package failed validation: Import test_cmd: FAIL (boom)"),
    )
    def test_gate_failure_scatters_nothing(
        self, mock_validate, mock_pip, mock_apt, mock_conflicts, mock_seed,
        mock_enable, tmp_path,
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": None})
        project_dir, packages_dir = self._sandbox(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            with pytest.raises(InstallError, match="failed validation"):
                install_from_local(repo)

        assert not (project_dir / "commands").exists(), "no component dirs on gate failure"
        assert list(packages_dir.iterdir()) == [], "no metadata/namespace/lib on gate failure"
        mock_seed.assert_not_called()
        mock_enable.assert_not_called()

    @patch("services.command_store_service._enable_in_registry")
    @patch("services.command_store_service._seed_secrets")
    @patch("services.command_store_service._check_name_conflicts")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._run_preflight_validation")
    def test_gate_success_proceeds_to_scatter(
        self, mock_validate, mock_pip, mock_apt, mock_conflicts, mock_seed,
        mock_enable, tmp_path,
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": None})
        project_dir, packages_dir = self._sandbox(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            manifest = install_from_local(repo)

        mock_validate.assert_called_once_with(repo)
        assert manifest.name == "test_cmd"
        assert (project_dir / "commands" / "custom_commands" / "test_cmd" / "command.py").exists()
        assert (packages_dir / "test_cmd.json").exists()

    @patch("services.command_store_service._enable_in_registry")
    @patch("services.command_store_service._seed_secrets")
    @patch("services.command_store_service._check_name_conflicts")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._run_preflight_validation")
    def test_skip_tests_bypasses_gate(
        self, mock_validate, mock_pip, mock_apt, mock_conflicts, mock_seed,
        mock_enable, tmp_path,
    ):
        repo = _create_fake_repo(tmp_path, {"min_jarvis_version": None})
        project_dir, packages_dir = self._sandbox(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            install_from_local(repo, skip_tests=True)

        mock_validate.assert_not_called()
        assert (packages_dir / "test_cmd.json").exists(), "install proceeds without the gate"


class TestRunPreflightValidation:
    """Unit tests for the subprocess wrapper itself (subprocess mocked)."""

    @patch("services.command_store_service.subprocess.run")
    def test_zero_exit_passes(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="All checks passed.\n", stderr="")
        _run_preflight_validation(tmp_path)  # should not raise

    @patch("services.command_store_service.subprocess.run")
    def test_invokes_command_store_validate_in_subprocess(self, mock_run, tmp_path):
        import sys as _sys
        from services import command_store_service as css

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _run_preflight_validation(tmp_path)

        argv = mock_run.call_args[0][0]
        assert argv == [
            _sys.executable,
            str(css._PROJECT_DIR / "scripts" / "command_store.py"),
            "validate",
            str(tmp_path),
        ]
        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == css._PROJECT_DIR
        assert kwargs["timeout"] == _PREFLIGHT_VALIDATE_TIMEOUT_SECONDS

    @patch("services.command_store_service.subprocess.run")
    def test_failure_surfaces_first_fail_line(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="Package: x v1.0.0\n  Import x: FAIL (No module named 'nope')\n",
            stderr="Some checks failed.\n",
        )
        with pytest.raises(InstallError, match=r"Package failed validation: .*No module named 'nope'"):
            _run_preflight_validation(tmp_path)

    @patch("services.command_store_service.subprocess.run")
    def test_failure_surfaces_stderr_fail_line(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="FAIL: Missing manifest\n",
        )
        with pytest.raises(InstallError, match="Package failed validation: FAIL: Missing manifest"):
            _run_preflight_validation(tmp_path)

    @patch("services.command_store_service.subprocess.run")
    def test_failure_without_fail_line_uses_last_stderr_line(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Traceback (most recent call last):\nValueError: kaboom\n",
        )
        with pytest.raises(InstallError, match="Package failed validation: ValueError: kaboom"):
            _run_preflight_validation(tmp_path)

    @patch("services.command_store_service.subprocess.run")
    def test_timeout_raises_install_error(self, mock_run, tmp_path):
        import subprocess as _sp
        mock_run.side_effect = _sp.TimeoutExpired(cmd=["validate"], timeout=180)
        with pytest.raises(InstallError, match="timed out"):
            _run_preflight_validation(tmp_path)


class TestValidatePackageStagedLayout:
    """validate_package must resolve shared-code imports from a staged dir."""

    def _staged_repo(
        self,
        tmp_path: Path,
        name: str,
        command_source: str,
        component_rel: str | None = None,
    ) -> Path:
        repo = tmp_path / "staged" / name
        component_rel = component_rel or f"commands/{name}/command.py"
        comp_file = repo / component_rel
        comp_file.parent.mkdir(parents=True)
        comp_file.write_text(command_source)

        manifest = {
            "name": name,
            "description": "staged test package",
            "version": "1.0.0",
            "components": [
                {"type": "command", "name": name, "path": component_rel},
            ],
        }
        (repo / "jarvis_package.yaml").write_text(yaml.dump(manifest))
        return repo

    def _command_source(self, name: str, extra_imports: str = "") -> str:
        return (
            f"{extra_imports}"
            "from jarvis_command_sdk import IJarvisCommand, CommandResponse, CommandExample, JarvisParameter\n"
            "\n"
            f"class StagedCmd(IJarvisCommand):\n"
            "    @property\n"
            f"    def command_name(self): return \"{name}\"\n"
            "    @property\n"
            "    def description(self): return \"Test\"\n"
            "    @property\n"
            "    def parameters(self): return []\n"
            "    @property\n"
            "    def required_secrets(self): return []\n"
            "    @property\n"
            "    def keywords(self): return [\"test\"]\n"
            "    def generate_prompt_examples(self): return [CommandExample(\"t\", {})]\n"
            "    def generate_adapter_examples(self): return self.generate_prompt_examples()\n"
            "    def run(self, ri, **kw): return CommandResponse.success_response({})\n"
        )

    def test_finds_shared_dir_package_from_repo_root(self, tmp_path):
        name = "ri_shared_root_cmd"
        repo = self._staged_repo(
            tmp_path, name,
            self._command_source(name, "from ri_demo_shared.helper import GREETING\n"),
        )
        shared = repo / "ri_demo_shared"
        shared.mkdir()
        (shared / "__init__.py").write_text("")
        (shared / "helper.py").write_text("GREETING = 'hi'\n")

        with patch("services.command_store_service.register_package_lib_paths"):
            result = validate_package(repo)

        assert result["imports"][name]["ok"] is True, result["imports"][name]

    def test_finds_modules_inside_shared_dir(self, tmp_path):
        """*_shared dirs themselves go on sys.path (email_shared convention)."""
        name = "ri_shared_mod_cmd"
        repo = self._staged_repo(
            tmp_path, name,
            self._command_source(name, "from ri_demo_helper_mod import VALUE\n"),
        )
        shared = repo / "ri_demo2_shared"
        shared.mkdir()
        (shared / "ri_demo_helper_mod.py").write_text("VALUE = 42\n")

        with patch("services.command_store_service.register_package_lib_paths"):
            result = validate_package(repo)

        assert result["imports"][name]["ok"] is True, result["imports"][name]

    def test_finds_modules_inside_lib_dir(self, tmp_path):
        """*_lib dirs (installed-layout staging) go on sys.path too."""
        name = "ri_lib_cmd"
        repo = self._staged_repo(
            tmp_path, name,
            self._command_source(name, "from ri_demo_lib_util import LIB_VALUE\n"),
        )
        lib = repo / f"{name}_lib"
        lib.mkdir()
        (lib / "ri_demo_lib_util.py").write_text("LIB_VALUE = 7\n")

        with patch("services.command_store_service.register_package_lib_paths"):
            result = validate_package(repo)

        assert result["imports"][name]["ok"] is True, result["imports"][name]

    def test_calls_register_package_lib_paths(self, tmp_path):
        """jarvis_dependencies namespaces must resolve during the gate."""
        name = "ri_register_cmd"
        repo = self._staged_repo(tmp_path, name, self._command_source(name))

        with patch("services.command_store_service.register_package_lib_paths") as mock_reg:
            validate_package(repo)

        mock_reg.assert_called_once()


class TestPreviousSnapshotOnUpdate:
    """Phase 4: updating an existing package writes a .previous rollback
    snapshot (and records previous_version in the new metadata); fresh
    installs don't."""

    def _sandbox(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir(exist_ok=True)
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir(exist_ok=True)
        return project_dir, packages_dir

    def _install(self, tmp_path, project_dir, packages_dir, version, repo_name):
        repo_parent = tmp_path / repo_name
        repo_parent.mkdir()
        repo = _create_fake_repo(repo_parent, {"version": version, "min_jarvis_version": None})

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir), \
             patch("services.command_store_service._check_name_conflicts"), \
             patch("services.command_store_service._install_apt_deps"), \
             patch("services.command_store_service._install_pip_deps"), \
             patch("services.command_store_service._run_preflight_validation"), \
             patch("services.command_store_service._seed_secrets"), \
             patch("services.command_store_service._enable_in_registry"):
            return install_from_local(repo)

    def test_fresh_install_writes_no_previous(self, tmp_path):
        project_dir, packages_dir = self._sandbox(tmp_path)

        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")

        assert not (packages_dir / "test_cmd" / ".previous").exists()
        meta = json.loads((packages_dir / "test_cmd.json").read_text())
        assert "previous_version" not in meta

    def test_update_writes_previous_snapshot(self, tmp_path):
        project_dir, packages_dir = self._sandbox(tmp_path)

        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")
        # Mutate the live component so we can prove .previous captured it.
        live_cmd = project_dir / "commands" / "custom_commands" / "test_cmd" / "command.py"
        v1_source = live_cmd.read_text()

        self._install(tmp_path, project_dir, packages_dir, "1.1.0", "clone2")

        prev_dir = packages_dir / "test_cmd" / ".previous"
        assert (prev_dir / "meta.json").exists()
        prev_meta = json.loads((prev_dir / "meta.json").read_text())
        assert prev_meta["version"] == "1.0.0"
        # Component dir copied under components/<type>__<name>/
        assert (prev_dir / "components" / "command__test_cmd" / "command.py").exists()
        assert (prev_dir / "components" / "command__test_cmd" / "command.py").read_text() == v1_source
        # Namespace __init__.py captured
        assert (prev_dir / "__init__.py").exists()
        # Atomic build leaves no staging dir behind
        assert not (packages_dir / "test_cmd" / ".previous.tmp").exists()
        # New metadata records the rollback target
        meta = json.loads((packages_dir / "test_cmd.json").read_text())
        assert meta["version"] == "1.1.0"
        assert meta["previous_version"] == "1.0.0"
        assert has_previous_version("test_cmd") is False  # unpatched PACKAGES_DIR
        with patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            assert has_previous_version("test_cmd") is True

    def test_second_update_replaces_previous_wholesale(self, tmp_path):
        project_dir, packages_dir = self._sandbox(tmp_path)

        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")
        self._install(tmp_path, project_dir, packages_dir, "1.1.0", "clone2")
        self._install(tmp_path, project_dir, packages_dir, "1.2.0", "clone3")

        prev_meta = json.loads(
            (packages_dir / "test_cmd" / ".previous" / "meta.json").read_text()
        )
        assert prev_meta["version"] == "1.1.0"  # N-1, not N-2
        meta = json.loads((packages_dir / "test_cmd.json").read_text())
        assert meta["previous_version"] == "1.1.0"

    def test_snapshot_failure_does_not_abort_install(self, tmp_path):
        project_dir, packages_dir = self._sandbox(tmp_path)

        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")

        with patch(
            "services.command_store_service._write_previous_snapshot",
            side_effect=OSError("disk full"),
        ):
            manifest = self._install(tmp_path, project_dir, packages_dir, "1.1.0", "clone2")

        assert manifest.version == "1.1.0"
        meta = json.loads((packages_dir / "test_cmd.json").read_text())
        assert meta["version"] == "1.1.0"
        assert "previous_version" not in meta

    def test_snapshot_copy_failure_leaves_no_previous(self, tmp_path):
        """A mid-snapshot copy failure must leave NO .previous at all —
        meta.json is written LAST, so a half-snapshot can never be mistaken
        for a usable rollback source."""
        project_dir, packages_dir = self._sandbox(tmp_path)
        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")

        with patch(
            "services.command_store_service.shutil.copytree",
            side_effect=OSError("disk full"),
        ):
            manifest = self._install(tmp_path, project_dir, packages_dir, "1.1.0", "clone2")

        # Install still proceeds (snapshot is best-effort)...
        assert manifest.version == "1.1.0"
        # ...but no half-snapshot or staging dir remains.
        assert not (packages_dir / "test_cmd" / ".previous").exists()
        assert not (packages_dir / "test_cmd" / ".previous.tmp").exists()
        meta = json.loads((packages_dir / "test_cmd.json").read_text())
        assert "previous_version" not in meta
        with patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            assert has_previous_version("test_cmd") is False

    def test_snapshot_copy_failure_preserves_prior_good_previous(self, tmp_path):
        """The prior good .previous survives until the new snapshot is fully
        built — a failed rebuild must not destroy the existing rollback
        source."""
        project_dir, packages_dir = self._sandbox(tmp_path)
        self._install(tmp_path, project_dir, packages_dir, "1.0.0", "clone1")
        self._install(tmp_path, project_dir, packages_dir, "1.1.0", "clone2")

        with patch(
            "services.command_store_service.shutil.copytree",
            side_effect=OSError("disk full"),
        ):
            self._install(tmp_path, project_dir, packages_dir, "1.2.0", "clone3")

        prev_meta = json.loads(
            (packages_dir / "test_cmd" / ".previous" / "meta.json").read_text()
        )
        assert prev_meta["version"] == "1.0.0"  # prior good snapshot intact
        assert not (packages_dir / "test_cmd" / ".previous.tmp").exists()


class TestRevertPackage:
    """Phase 4: revert_package restores the .previous snapshot."""

    def _setup_installed_with_previous(self, tmp_path):
        project_dir = tmp_path / "project"
        packages_dir = tmp_path / "packages"

        # Live (bad) v2 component
        cmd_dir = project_dir / "commands" / "custom_commands" / "foo"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "command.py").write_text("# v2")

        # Current metadata (v2)
        packages_dir.mkdir(parents=True)
        current_meta = {
            "package_name": "foo",
            "version": "2.0.0",
            "previous_version": "1.0.0",
            "components": [
                {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
            ],
            "component_dirs": {"command:foo": str(cmd_dir)},
            "pip_packages": [],
        }
        (packages_dir / "foo.json").write_text(json.dumps(current_meta))

        # Current lib + namespace (v2)
        pkg_dir = packages_dir / "foo"
        (pkg_dir / "foo_lib").mkdir(parents=True)
        (pkg_dir / "foo_lib" / "helper.py").write_text("VERSION = 2")
        (pkg_dir / "__init__.py").write_text("# v2 namespace")

        # .previous snapshot (v1)
        prev = pkg_dir / ".previous"
        prev_meta = {
            "package_name": "foo",
            "version": "1.0.0",
            "components": [
                {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
            ],
            "component_dirs": {"command:foo": str(cmd_dir)},
            "pip_packages": [{"name": "httpx", "version": "0.27.0"}],
        }
        (prev / "components" / "command__foo").mkdir(parents=True)
        (prev / "components" / "command__foo" / "command.py").write_text("# v1")
        (prev / "foo_lib").mkdir()
        (prev / "foo_lib" / "helper.py").write_text("VERSION = 1")
        (prev / "__init__.py").write_text("# v1 namespace")
        (prev / "meta.json").write_text(json.dumps(prev_meta))

        return project_dir, packages_dir, cmd_dir

    @patch("services.command_store_service.subprocess.run")
    def test_revert_restores_previous_version(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        project_dir, packages_dir, cmd_dir = self._setup_installed_with_previous(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            restored = revert_package("foo")

        assert restored == "1.0.0"
        assert (cmd_dir / "command.py").read_text() == "# v1"
        assert (packages_dir / "foo" / "foo_lib" / "helper.py").read_text() == "VERSION = 1"
        assert (packages_dir / "foo" / "__init__.py").read_text() == "# v1 namespace"
        meta = json.loads((packages_dir / "foo.json").read_text())
        assert meta["version"] == "1.0.0"
        # Revert consumes the snapshot — no dangling "Revert to ..." offers.
        assert "previous_version" not in meta
        assert not (packages_dir / "foo" / ".previous").exists()
        # Best-effort pip install of the restored deps
        argv = mock_run.call_args[0][0]
        assert "httpx==0.27.0" in argv

    @patch("services.command_store_service.subprocess.run")
    def test_revert_pip_failure_is_non_fatal(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="resolver exploded")
        project_dir, packages_dir, cmd_dir = self._setup_installed_with_previous(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            restored = revert_package("foo")

        assert restored == "1.0.0"
        assert (cmd_dir / "command.py").read_text() == "# v1"

    def test_revert_raises_on_missing_component_source(self, tmp_path):
        """Defense in depth: the atomic snapshot writer means meta.json only
        exists for complete snapshots, so a recorded component missing from
        .previous/components/ is corruption — refuse rather than silently
        restore a partial package."""
        project_dir, packages_dir, cmd_dir = self._setup_installed_with_previous(tmp_path)
        shutil.rmtree(packages_dir / "foo" / ".previous" / "components" / "command__foo")

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            with pytest.raises(RevertError, match="incomplete"):
                revert_package("foo")

    def test_revert_without_metadata_raises(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            with pytest.raises(RevertError, match="not installed"):
                revert_package("ghost")

    def test_revert_without_previous_raises(self, tmp_path):
        packages_dir = tmp_path / "packages"
        packages_dir.mkdir()
        (packages_dir / "foo.json").write_text(json.dumps({
            "package_name": "foo", "version": "2.0.0",
        }))

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            with pytest.raises(RevertError, match="No previous version"):
                revert_package("foo")

    def test_has_previous_version(self, tmp_path):
        packages_dir = tmp_path / "packages"
        (packages_dir / "foo" / ".previous").mkdir(parents=True)

        with patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            assert has_previous_version("foo") is False  # no meta.json yet
            (packages_dir / "foo" / ".previous" / "meta.json").write_text("{}")
            assert has_previous_version("foo") is True
            assert has_previous_version("bar") is False

    def test_remove_drops_previous_snapshot(self, tmp_path):
        """Uninstall must not leave an orphaned .previous for a future
        fresh install to 'roll back' to."""
        project_dir, packages_dir, cmd_dir = self._setup_installed_with_previous(tmp_path)

        with patch("services.command_store_service._PROJECT_DIR", project_dir), \
             patch("services.command_store_service.PACKAGES_DIR", packages_dir):
            remove("foo")

        assert not (packages_dir / "foo" / ".previous").exists()
        assert not (packages_dir / "foo.json").exists()


class TestInstallAptBeforePipOrdering:
    @patch("services.command_store_service._clone_repo")
    @patch("services.command_store_service._check_name_conflict")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._seed_secrets")
    def test_apt_called_before_pip_in_do_install(
        self, mock_seed, mock_pip, mock_apt, mock_conflict, mock_clone, tmp_path
    ):
        repo_parent = tmp_path / "clone"
        repo_parent.mkdir()
        repo = _create_fake_repo(repo_parent)
        mock_clone.return_value = repo

        parent = MagicMock()
        parent.attach_mock(mock_apt, "_install_apt_deps")
        parent.attach_mock(mock_pip, "_install_pip_deps")

        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            install_from_github("https://github.com/test/repo", skip_tests=True)

        names = [c[0] for c in parent.mock_calls if c[0] in ("_install_apt_deps", "_install_pip_deps")]
        assert "_install_apt_deps" in names and "_install_pip_deps" in names
        assert names.index("_install_apt_deps") < names.index("_install_pip_deps")

    @patch("services.command_store_service._clone_repo")
    @patch("services.command_store_service._check_name_conflict")
    @patch("services.command_store_service._install_apt_deps")
    @patch("services.command_store_service._install_pip_deps")
    @patch("services.command_store_service._seed_secrets")
    def test_apt_failure_aborts_before_pip(
        self, mock_seed, mock_pip, mock_apt, mock_conflict, mock_clone, tmp_path
    ):
        repo_parent = tmp_path / "clone"
        repo_parent.mkdir()
        repo = _create_fake_repo(repo_parent)
        mock_clone.return_value = repo
        mock_apt.side_effect = InstallError("apt failed")

        test_custom_dir = tmp_path / "custom_commands"
        test_custom_dir.mkdir()

        with patch("services.command_store_service.CUSTOM_COMMANDS_DIR", test_custom_dir):
            with pytest.raises(InstallError, match="apt failed"):
                install_from_github("https://github.com/test/repo", skip_tests=True)

        mock_pip.assert_not_called()
