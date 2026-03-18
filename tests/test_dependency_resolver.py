"""Tests for the dependency resolver.

TDD — written before the implementation.
"""

import os
import tempfile

import pytest

from core.ijarvis_package import JarvisPackage


# ── Parse requirements file ──────────────────────────────────────────


class TestParseRequirementsFile:
    def test_simple_names(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\nrequests\nnumpy\n")
        result = parse_requirements_file(str(req))
        assert result == {"httpx": "", "requests": "", "numpy": ""}

    def test_version_constraints(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text("httpx>=0.24\nrequests==2.31.0\nnumpy<2\n")
        result = parse_requirements_file(str(req))
        assert result == {"httpx": ">=0.24", "requests": "==2.31.0", "numpy": "<2"}

    def test_skips_comments_and_blanks(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text("# comment\nhttpx\n\n  # another comment\nrequests\n")
        result = parse_requirements_file(str(req))
        assert result == {"httpx": "", "requests": ""}

    def test_skips_url_based_deps(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text(
            "httpx\n"
            "jarvis-log-client @ git+https://github.com/user/repo.git\n"
            "onnxruntime @ https://example.com/wheel.whl\n"
            "requests\n"
        )
        result = parse_requirements_file(str(req))
        assert result == {"httpx": "", "requests": ""}

    def test_complex_constraints(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text("cryptography>=41.0\nonnxruntime>=1.10.0,<2\n")
        result = parse_requirements_file(str(req))
        assert result == {
            "cryptography": ">=41.0",
            "onnxruntime": ">=1.10.0,<2",
        }

    def test_normalizes_package_names(self, tmp_path):
        from utils.dependency_resolver import parse_requirements_file

        req = tmp_path / "requirements.txt"
        req.write_text("paho-mqtt\nPyAudio\nscikit_learn\n")
        result = parse_requirements_file(str(req))
        # All names should be normalized to lowercase with hyphens
        assert "paho-mqtt" in result
        assert "pyaudio" in result
        assert "scikit-learn" in result


# ── Collect packages from commands ───────────────────────────────────


class _FakeCommand:
    """Minimal fake command for testing."""

    def __init__(self, name: str, packages: list[JarvisPackage]):
        self._name = name
        self._packages = packages

    @property
    def command_name(self) -> str:
        return self._name

    @property
    def required_packages(self) -> list[JarvisPackage]:
        return self._packages


class TestCollectCommandPackages:
    def test_empty_commands(self):
        from utils.dependency_resolver import collect_command_packages

        result = collect_command_packages({})
        assert result == {}

    def test_no_packages(self):
        from utils.dependency_resolver import collect_command_packages

        commands = {"cmd1": _FakeCommand("cmd1", [])}
        result = collect_command_packages(commands)
        assert result == {}

    def test_single_command_single_package(self):
        from utils.dependency_resolver import collect_command_packages

        commands = {
            "news": _FakeCommand("news", [JarvisPackage("feedparser")]),
        }
        result = collect_command_packages(commands)
        assert "feedparser" in result
        assert ("news", "") in result["feedparser"]

    def test_multiple_commands_same_package(self):
        from utils.dependency_resolver import collect_command_packages

        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("httpx", ">=0.24")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("httpx", ">=0.20")]),
        }
        result = collect_command_packages(commands)
        assert "httpx" in result
        assert len(result["httpx"]) == 2

    def test_grouped_by_normalized_name(self):
        from utils.dependency_resolver import collect_command_packages

        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("music-assistant-client", ">=1.3.0")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("music_assistant_client", ">=1.0.0")]),
        }
        result = collect_command_packages(commands)
        assert "music-assistant-client" in result
        assert len(result["music-assistant-client"]) == 2


# ── Compatibility checks ────────────────────────────────────────────


class TestCheckCompatibility:
    def test_unconstrained_compatible(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility(["", ""]) is True

    def test_single_spec_compatible(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility([">=1.0"]) is True

    def test_overlapping_ranges_compatible(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility([">=1.0,<3.0", ">=2.0,<4.0"]) is True

    def test_disjoint_ranges_conflict(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility([">=3.0", "<2.0"]) is False

    def test_pinned_versions_same(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility(["==1.5.0", "==1.5.0"]) is True

    def test_pinned_versions_different(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility(["==1.5.0", "==2.0.0"]) is False

    def test_pinned_within_range(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility(["==1.5.0", ">=1.0,<2.0"]) is True

    def test_pinned_outside_range(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility(["==3.0.0", ">=1.0,<2.0"]) is False

    def test_empty_specs_compatible(self):
        from utils.dependency_resolver import check_compatibility

        assert check_compatibility([]) is True


# ── Full resolution ──────────────────────────────────────────────────


class TestResolveAll:
    def test_no_command_packages(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\nrequests\n")
        result = resolve_all(str(req), {})
        assert result.success is True
        assert result.conflicts == []
        assert result.merged_specs == []

    def test_unique_packages_succeed(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        commands = {
            "news": _FakeCommand("news", [JarvisPackage("feedparser")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is True
        assert "feedparser" in result.merged_specs

    def test_compatible_overlap_succeeds(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("aiohttp", ">=3.0")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("aiohttp", ">=3.5,<4.0")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is True
        # The merged spec should contain all constraints
        aiohttp_specs = [s for s in result.merged_specs if s.startswith("aiohttp")]
        assert len(aiohttp_specs) == 1

    def test_conflicts_fail(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("aiohttp", ">=4.0")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("aiohttp", "<3.0")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is False
        assert len(result.conflicts) == 1
        assert result.conflicts[0].package_name == "aiohttp"

    def test_base_command_overlap_compatible(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx>=0.24\n")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("httpx", ">=0.20")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is True
        # httpx is in base, so it should NOT appear in merged_specs
        httpx_specs = [s for s in result.merged_specs if s.startswith("httpx")]
        assert len(httpx_specs) == 0

    def test_base_command_overlap_conflict(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx>=0.24\n")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("httpx", "<0.1")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is False
        assert len(result.conflicts) == 1
        assert result.conflicts[0].package_name == "httpx"

    def test_snapshot_populated(self, tmp_path):
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("httpx\n")
        commands = {
            "news": _FakeCommand("news", [JarvisPackage("feedparser")]),
        }
        result = resolve_all(str(req), commands)
        assert "commands" in result.snapshot
        assert "news" in result.snapshot["commands"]
        assert "resolved_at" in result.snapshot

    def test_version_with_pinned_spec(self, tmp_path):
        """JarvisPackage("pkg", "1.0.0") should be treated as ==1.0.0"""
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("somepkg", "1.0.0")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("somepkg", ">=1.0,<2.0")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is True

    def test_version_pinned_conflict(self, tmp_path):
        """JarvisPackage("pkg", "3.0.0") conflicts with <2.0"""
        from utils.dependency_resolver import resolve_all

        req = tmp_path / "requirements.txt"
        req.write_text("")
        commands = {
            "cmd1": _FakeCommand("cmd1", [JarvisPackage("somepkg", "3.0.0")]),
            "cmd2": _FakeCommand("cmd2", [JarvisPackage("somepkg", "<2.0")]),
        }
        result = resolve_all(str(req), commands)
        assert result.success is False
