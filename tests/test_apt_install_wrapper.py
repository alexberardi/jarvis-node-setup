"""Tests for the scripts/jarvis-apt-install wrapper.

Exercises the wrapper script under a $PATH-shimmed apt-get + nice so the
regex shape guard and argv pass-through can be verified without invoking
real apt-get.
"""

import os
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = _REPO_ROOT / "scripts" / "jarvis-apt-install"


@pytest.fixture
def apt_get_stub(tmp_path):
    """(stub_dir, log_path) — $PATH dir with apt-get + nice stubs that log argv."""
    log_path = tmp_path / "apt_get.log"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    apt = stub_dir / "apt-get"
    apt.write_text(f'#!/bin/sh\necho "$@" >> {log_path}\nexit 0\n')
    apt.chmod(0o755)
    # nice -n 15 apt-get ... → strip the first two args (-n 15), exec the rest.
    nice = stub_dir / "nice"
    nice.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    nice.chmod(0o755)
    return stub_dir, log_path


def _run(args, apt_get_stub, timeout=5):
    stub_dir, log_path = apt_get_stub
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
    def test_valid_lowercase_name_invokes_apt_get(self, apt_get_stub):
        result, logged = _run(["mpv"], apt_get_stub)
        assert result.returncode == 0, result.stderr
        assert len(logged) == 1
        argv = logged[0].split()
        assert argv == ["-o", "DPkg::Lock::Timeout=60", "install", "-y",
                        "--no-install-recommends", "mpv"]

    def test_multiple_valid_names_all_passed_through(self, apt_get_stub):
        result, logged = _run(["mpv", "alsa-utils", "ffmpeg"], apt_get_stub)
        assert result.returncode == 0, result.stderr
        argv = logged[0].split()
        # Order preserved
        assert argv[-3:] == ["mpv", "alsa-utils", "ffmpeg"]
        assert "install" in argv and "-y" in argv

    @pytest.mark.parametrize("name", [
        "libstdc++6", "g++-10", "python3.11", "lib-foo.bar+baz-1",
    ])
    def test_valid_names_with_allowed_metacharacters_accepted(self, apt_get_stub, name):
        result, logged = _run([name], apt_get_stub)
        assert result.returncode == 0, result.stderr
        argv = logged[0].split()
        assert name in argv


class TestWrapperEdgeCases:
    def test_invalid_name_uppercase_rejected(self, apt_get_stub):
        result, logged = _run(["MPV"], apt_get_stub)
        assert result.returncode != 0
        assert "invalid" in result.stderr.lower()
        assert "MPV" in result.stderr
        assert logged == []

    def test_invalid_name_starts_with_digit_rejected(self, apt_get_stub):
        result, logged = _run(["7zip"], apt_get_stub)
        assert result.returncode != 0
        assert logged == []

    @pytest.mark.parametrize("name", [
        "mpv_player",
        "../etc/passwd",
        "mpv;rm -rf /",
        "mpv$(id)",
        "mpv space",
        "pkg|cat",
    ])
    def test_invalid_name_with_disallowed_chars_rejected(self, apt_get_stub, name):
        result, logged = _run([name], apt_get_stub)
        assert result.returncode != 0
        assert logged == []

    def test_first_invalid_name_aborts_before_apt_get(self, apt_get_stub):
        result, logged = _run(["mpv", "BADNAME", "alsa-utils"], apt_get_stub)
        assert result.returncode != 0
        assert logged == []

    def test_empty_argument_rejected(self, apt_get_stub):
        result, logged = _run([""], apt_get_stub)
        assert result.returncode != 0
        assert logged == []

    def test_no_args_does_not_invoke_apt_get(self, apt_get_stub):
        result, logged = _run([], apt_get_stub)
        # Short-circuit to exit 0 without invoking apt-get
        assert result.returncode == 0
        assert logged == []


class TestWrapperErrorFlows:
    def test_wrapper_handles_apt_get_lock_contention(self, tmp_path):
        """If apt-get exits 100 (dpkg lock), the wrapper surfaces that code without hanging."""
        log_path = tmp_path / "apt_get.log"
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        apt = stub_dir / "apt-get"
        apt.write_text(
            f'#!/bin/sh\necho "$@" >> {log_path}\nsleep 2\nexit 100\n'
        )
        apt.chmod(0o755)
        nice = stub_dir / "nice"
        nice.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
        nice.chmod(0o755)
        result = subprocess.run(
            [str(WRAPPER), "mpv"],
            env={**os.environ, "PATH": f"{stub_dir}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
