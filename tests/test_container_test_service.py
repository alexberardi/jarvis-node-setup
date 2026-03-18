"""Tests for container_test_service — Docker-based command validation."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from services.container_test_service import (
    run_container_tests,
    _build_dockerfile,
    _check_docker_available,
    ContainerTestResult,
)


class TestBuildDockerfile:
    def test_no_packages(self):
        df = _build_dockerfile([])
        assert "FROM python:3.11-slim" in df
        assert "pip install --no-cache-dir /test/sdk/" in df
        assert "test_harness.py" in df

    def test_with_packages(self):
        df = _build_dockerfile(["requests", "httpx>=0.25"])
        assert "pip install --no-cache-dir requests httpx>=0.25" in df


class TestCheckDockerAvailable:
    @patch("subprocess.run")
    def test_docker_available(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert _check_docker_available() is True

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_docker_not_installed(self, mock_run):
        assert _check_docker_available() is False


class TestRunContainerTests:
    @patch("services.container_test_service._check_docker_available", return_value=False)
    def test_skip_when_no_docker(self, mock_docker, tmp_path):
        result = run_container_tests(tmp_path)
        assert result.passed is True
        assert "SKIP" in result.summary

    @patch("services.container_test_service._check_docker_available", return_value=True)
    @patch("subprocess.run")
    def test_build_failure(self, mock_run, mock_docker, tmp_path):
        # Create minimal command dir
        (tmp_path / "command.py").write_text("# test")
        (tmp_path / "jarvis_command.yaml").write_text("name: test")

        # Build fails
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Build error: syntax error",
        )

        result = run_container_tests(tmp_path)
        assert result.passed is False
        assert "build failed" in result.summary.lower()

    @patch("services.container_test_service._check_docker_available", return_value=True)
    @patch("subprocess.run")
    def test_successful_run(self, mock_run, mock_docker, tmp_path):
        (tmp_path / "command.py").write_text("# test")
        (tmp_path / "jarvis_command.yaml").write_text("name: test")

        # First call = build (success), second call = run (success)
        test_output = json.dumps({
            "passed": 10,
            "failed": 0,
            "summary": "PASS - 10/10 tests passed",
            "tests": [],
            "errors": [],
        })

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # build
            MagicMock(returncode=0, stdout=test_output, stderr=""),  # run
            MagicMock(returncode=0),  # rmi cleanup
        ]

        result = run_container_tests(tmp_path)
        assert result.passed is True
        assert result.pass_count == 10
        assert result.fail_count == 0

    @patch("services.container_test_service._check_docker_available", return_value=True)
    @patch("subprocess.run")
    def test_failed_tests(self, mock_run, mock_docker, tmp_path):
        (tmp_path / "command.py").write_text("# test")
        (tmp_path / "jarvis_command.yaml").write_text("name: test")

        test_output = json.dumps({
            "passed": 8,
            "failed": 2,
            "summary": "FAIL - 8/10 tests passed",
            "tests": [],
            "errors": ["command_name: returned empty string"],
        })

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # build
            MagicMock(returncode=1, stdout=test_output, stderr=""),  # run
            MagicMock(returncode=0),  # rmi
        ]

        result = run_container_tests(tmp_path)
        assert result.passed is False
        assert result.fail_count == 2
        assert len(result.errors) == 1

    @patch("services.container_test_service._check_docker_available", return_value=True)
    @patch("subprocess.run")
    def test_unparseable_output(self, mock_run, mock_docker, tmp_path):
        (tmp_path / "command.py").write_text("# test")
        (tmp_path / "jarvis_command.yaml").write_text("name: test")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # build
            MagicMock(returncode=1, stdout="not json", stderr=""),  # run
            MagicMock(returncode=0),  # rmi
        ]

        result = run_container_tests(tmp_path)
        assert result.passed is False
        assert "parse" in result.summary.lower()
