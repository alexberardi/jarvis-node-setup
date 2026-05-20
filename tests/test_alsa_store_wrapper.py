"""Tests for the scripts/jarvis-alsa-store wrapper.

Exercises the wrapper script under a $PATH-shimmed alsactl so we can verify
arg handling without actually writing to /var/lib/alsa/.
"""

import os
import subprocess
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = _REPO_ROOT / "scripts" / "jarvis-alsa-store"


def _stub_alsactl(tmp_path):
    """(stub_dir, log_path) — $PATH dir with an alsactl stub that logs argv."""
    log_path = tmp_path / "alsactl.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    alsa = stub_dir / "alsactl"
    alsa.write_text(f'#!/bin/sh\necho "$@" >> {log_path}\nexit 0\n')
    alsa.chmod(0o755)
    return stub_dir, log_path


def _run(args, tmp_path, timeout=5):
    stub_dir, log_path = _stub_alsactl(tmp_path)
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
    def test_zero_args_invokes_alsactl_store(self, tmp_path):
        result, logged = _run([], tmp_path)
        assert result.returncode == 0, result.stderr
        assert logged == ["store"], f"Expected single 'store' invocation, got: {logged}"


class TestWrapperEdgeCases:
    def test_any_arg_rejected(self, tmp_path):
        """The wrapper takes no arguments — defense against scope creep."""
        result, logged = _run(["restore"], tmp_path)
        assert result.returncode != 0
        assert "usage" in result.stderr.lower()
        assert logged == [], "alsactl must not be invoked when args are rejected"

    def test_multiple_args_rejected(self, tmp_path):
        result, logged = _run(["store", "extra"], tmp_path)
        assert result.returncode != 0
        assert logged == []

    def test_empty_arg_rejected(self, tmp_path):
        """Even a single empty-string arg should be rejected — strict shape guard."""
        result, logged = _run([""], tmp_path)
        assert result.returncode != 0
        assert logged == []
