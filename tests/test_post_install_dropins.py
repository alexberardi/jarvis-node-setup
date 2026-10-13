"""Tests for services/post_install_dropins — cleanup + startup self-heal of
systemd drop-ins written by Pantry packages' post_install ops.

Background (2026-10): jarvis-dev's mpd crash-looped every ~5 s (5,067
restarts) because the audacy package's drop-in
(/etc/systemd/system/mpd.service.d/jarvis.conf) outlived the package: a
factory reset wiped ~/.jarvis/packages/ without asking the root-owned
wrapper to remove the drop-ins, so MPDCONF pointed at a deleted file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from services import post_install_dropins as pid


AUDACY_DROPIN = """\
# managed-by: jarvis-package audacy
[Unit]
Wants=user@1000.service
After=user@1000.service

[Service]
User=pi
Group=audio
Environment="MPDCONF={mpdconf}"
Environment="XDG_RUNTIME_DIR=/run/user/1000"
Environment="PULSE_RUNTIME_PATH=/run/user/1000/pulse"
Restart=on-failure
RestartSec=5
"""


def _write_dropin(root: Path, service: str, content: str, name: str = "jarvis.conf") -> Path:
    d = root / f"{service}.service.d"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content)
    return p


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    dropin_root = tmp_path / "etc-systemd-system"
    dropin_root.mkdir()
    packages_dir = tmp_path / "home" / ".jarvis" / "packages"
    packages_dir.mkdir(parents=True)
    return dropin_root, packages_dir


class TestFindStaleManagedDropins:
    def test_package_metadata_missing_is_stale(self, tree):
        dropin_root, packages_dir = tree
        mpdconf = packages_dir / "audacy" / "audacy_lib" / "audacy_mpd.conf"
        _write_dropin(dropin_root, "mpd", AUDACY_DROPIN.format(mpdconf=mpdconf))

        stale = pid.find_stale_managed_dropins(dropin_root, packages_dir)

        assert list(stale) == ["audacy"]
        reasons = " ".join(stale["audacy"])
        assert "not installed" in reasons
        assert "MPDCONF" in reasons  # the dangling path is named too

    def test_installed_package_with_missing_config_path_is_stale(self, tree):
        # The jarvis-dev shape after a partial cleanup: metadata still there,
        # but the file the drop-in points mpd at is gone.
        dropin_root, packages_dir = tree
        (packages_dir / "audacy.json").write_text("{}")
        mpdconf = packages_dir / "audacy" / "audacy_lib" / "audacy_mpd.conf"
        _write_dropin(dropin_root, "mpd", AUDACY_DROPIN.format(mpdconf=mpdconf))

        stale = pid.find_stale_managed_dropins(dropin_root, packages_dir)

        assert list(stale) == ["audacy"]
        assert any(str(mpdconf) in r for r in stale["audacy"])

    def test_healthy_dropin_is_not_stale(self, tree):
        dropin_root, packages_dir = tree
        (packages_dir / "audacy.json").write_text("{}")
        mpdconf = packages_dir / "audacy" / "audacy_lib" / "audacy_mpd.conf"
        mpdconf.parent.mkdir(parents=True)
        mpdconf.write_text("music_directory \"/tmp\"\n")
        _write_dropin(dropin_root, "mpd", AUDACY_DROPIN.format(mpdconf=mpdconf))

        assert pid.find_stale_managed_dropins(dropin_root, packages_dir) == {}

    def test_paths_outside_packages_dir_are_not_checked(self, tree):
        # /run/user/1000 may not exist when this runs (e.g. before login);
        # only package-shipped files decide staleness.
        dropin_root, packages_dir = tree
        (packages_dir / "music.json").write_text("{}")
        _write_dropin(
            dropin_root, "shairport-sync",
            "# managed-by: jarvis-package music\n[Service]\n"
            'Environment="XDG_RUNTIME_DIR=/nonexistent/run/user/1000"\n',
        )
        assert pid.find_stale_managed_dropins(dropin_root, packages_dir) == {}

    def test_unmanaged_dropins_are_ignored(self, tree):
        dropin_root, packages_dir = tree
        _write_dropin(dropin_root, "getty@tty1", "[Service]\nExecStart=\n", name="autologin.conf")
        _write_dropin(dropin_root, "mpd", "[Service]\nUser=pi\n", name="override.conf")
        assert pid.find_stale_managed_dropins(dropin_root, packages_dir) == {}

    def test_unsafe_package_name_in_marker_is_ignored(self, tree):
        # The name ends up on a sudo command line; never pass anything the
        # wrapper's own validator would reject.
        dropin_root, packages_dir = tree
        _write_dropin(dropin_root, "mpd", "# managed-by: jarvis-package ../evil x\n[Service]\n")
        assert pid.find_stale_managed_dropins(dropin_root, packages_dir) == {}

    def test_missing_dropin_root_is_empty(self, tmp_path):
        assert pid.find_stale_managed_dropins(tmp_path / "nope", tmp_path) == {}

    def test_unreadable_dropin_is_skipped(self, tree, monkeypatch):
        dropin_root, packages_dir = tree
        p = _write_dropin(dropin_root, "mpd", "# managed-by: jarvis-package audacy\n")
        real_read_text = Path.read_text

        def boom(self: Path, *a, **kw):
            if self == p:
                raise PermissionError("denied")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        assert pid.find_stale_managed_dropins(dropin_root, packages_dir) == {}


class TestRemoveManagedDropins:
    def test_invokes_wrapper_via_sudo(self, tmp_path, monkeypatch):
        wrapper = tmp_path / "jarvis-post-install"
        wrapper.write_text("#!/bin/sh\n")
        monkeypatch.setattr(pid, "WRAPPER_PATH", wrapper)
        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="removed 1", stderr="")

        monkeypatch.setattr(pid.subprocess, "run", fake_run)

        assert pid.remove_managed_dropins("audacy") is True
        assert calls == [["sudo", "-n", str(wrapper), "--package", "audacy", "remove-managed-dropins"]]

    def test_missing_wrapper_returns_false_without_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pid, "WRAPPER_PATH", tmp_path / "absent")
        monkeypatch.setattr(pid.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
        assert pid.remove_managed_dropins("audacy") is False

    def test_wrapper_failure_returns_false(self, tmp_path, monkeypatch):
        wrapper = tmp_path / "jarvis-post-install"
        wrapper.write_text("")
        monkeypatch.setattr(pid, "WRAPPER_PATH", wrapper)
        monkeypatch.setattr(
            pid.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="sudo: a password is required"),
        )
        assert pid.remove_managed_dropins("audacy") is False

    def test_timeout_returns_false(self, tmp_path, monkeypatch):
        wrapper = tmp_path / "jarvis-post-install"
        wrapper.write_text("")
        monkeypatch.setattr(pid, "WRAPPER_PATH", wrapper)

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)

        monkeypatch.setattr(pid.subprocess, "run", slow)
        assert pid.remove_managed_dropins("audacy") is False

    def test_rejects_unsafe_package_name(self, tmp_path, monkeypatch):
        wrapper = tmp_path / "jarvis-post-install"
        wrapper.write_text("")
        monkeypatch.setattr(pid, "WRAPPER_PATH", wrapper)
        monkeypatch.setattr(pid.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
        assert pid.remove_managed_dropins("a b; reboot") is False


class TestHealStaleDropins:
    def test_removes_only_stale_packages(self, tree, monkeypatch):
        dropin_root, packages_dir = tree
        # Stale: audacy gone.
        _write_dropin(
            dropin_root, "mpd",
            AUDACY_DROPIN.format(mpdconf=packages_dir / "audacy" / "x.conf"),
        )
        # Healthy: music installed.
        (packages_dir / "music.json").write_text("{}")
        _write_dropin(dropin_root, "shairport-sync", "# managed-by: jarvis-package music\n[Service]\n")

        removed: list[str] = []
        monkeypatch.setattr(pid, "remove_managed_dropins", lambda p: removed.append(p) or True)

        healed = pid.heal_stale_dropins(dropin_root, packages_dir)

        assert healed == ["audacy"]
        assert removed == ["audacy"]

    def test_failed_removal_is_not_reported_healed(self, tree, monkeypatch):
        dropin_root, packages_dir = tree
        _write_dropin(dropin_root, "mpd", "# managed-by: jarvis-package audacy\n[Service]\n")
        monkeypatch.setattr(pid, "remove_managed_dropins", lambda p: False)
        assert pid.heal_stale_dropins(dropin_root, packages_dir) == []

    def test_never_raises(self, tree, monkeypatch):
        dropin_root, packages_dir = tree

        def boom(*a, **k):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(pid, "find_stale_managed_dropins", boom)
        assert pid.heal_stale_dropins(dropin_root, packages_dir) == []

    def test_noop_when_nothing_stale(self, tree, monkeypatch):
        dropin_root, packages_dir = tree
        monkeypatch.setattr(pid, "remove_managed_dropins", lambda p: pytest.fail("called"))
        assert pid.heal_stale_dropins(dropin_root, packages_dir) == []


class TestFactoryResetRemovesDropins:
    """factory_reset wipes ~/.jarvis/packages wholesale — the leak path. It
    must first ask the wrapper to remove every package's drop-ins."""

    def test_clear_pantry_packages_removes_dropins_before_wiping(self, tmp_path, monkeypatch):
        from provisioning import factory_reset as fr

        home = tmp_path / "home"
        packages_dir = home / ".jarvis" / "packages"
        packages_dir.mkdir(parents=True)
        (packages_dir / "audacy.json").write_text("{}")
        (packages_dir / "music.json").write_text("{}")
        (packages_dir / "audacy").mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(fr, "_PROJECT_DIR", tmp_path / "project")

        seen: list[tuple[str, bool]] = []

        def fake_remove(pkg: str) -> bool:
            # Metadata must still be present when the wrapper runs.
            seen.append((pkg, (packages_dir / f"{pkg}.json").exists()))
            return True

        monkeypatch.setattr(fr, "remove_managed_dropins", fake_remove)

        cleared = fr._clear_pantry_packages()

        assert sorted(seen) == [("audacy", True), ("music", True)]
        assert not packages_dir.exists()
        assert "pantry:dropins:audacy" in cleared
        assert "pantry:packages_metadata" in cleared

    def test_dropin_removal_failure_does_not_block_reset(self, tmp_path, monkeypatch):
        from provisioning import factory_reset as fr

        home = tmp_path / "home"
        packages_dir = home / ".jarvis" / "packages"
        packages_dir.mkdir(parents=True)
        (packages_dir / "audacy.json").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setattr(fr, "_PROJECT_DIR", tmp_path / "project")

        def boom(pkg: str) -> bool:
            raise RuntimeError("wrapper exploded")

        monkeypatch.setattr(fr, "remove_managed_dropins", boom)

        fr._clear_pantry_packages()
        assert not packages_dir.exists()


class TestUninstallUsesSharedHelper:
    def test_remove_calls_remove_managed_dropins(self, monkeypatch):
        from services import command_store_service as css

        removed: list[str] = []
        monkeypatch.setattr(css, "remove_managed_dropins", lambda p: removed.append(p) or True)
        css._remove_post_install_dropins("audacy")
        assert removed == ["audacy"]


def test_main_starts_the_selfheal_at_boot():
    # main() is too entangled to run in a unit test; pin the wiring.
    src = (Path(__file__).resolve().parent.parent / "scripts" / "main.py").read_text()
    assert "from services.post_install_dropins import heal_stale_dropins" in src
    assert "target=heal_stale_dropins" in src
