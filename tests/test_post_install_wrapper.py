"""Tests for the scripts/jarvis-post-install wrapper.

The wrapper has no .py extension so import is done via SourceFileLoader.
We exercise the helpers directly (libconfig upsert, drop-in rendering,
path normalization) so the per-op dispatchers and CLI live on a tested
core.

Integration tests that actually invoke systemctl are intentionally out
of scope — they require the wrapper to be installed at /usr/local/sbin
with sudoers granted and run on a real system.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "jarvis-post-install"
)


@pytest.fixture(scope="module")
def wrapper():
    loader = importlib.machinery.SourceFileLoader("jpi", str(WRAPPER_PATH))
    spec = importlib.util.spec_from_loader("jpi", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ── _libconfig_upsert ───────────────────────────────────────────────────


class TestLibconfigUpsert:
    def test_inserts_new_key_into_existing_section(self, wrapper):
        src = 'general =\n{\n//\toutput_backend = "alsa";\n};\n'
        out = wrapper._libconfig_upsert(src, "general", "output_backend", "pa")
        assert 'output_backend = "pa"' in out
        # Commented template line should remain — we only touch active lines.
        assert "//\toutput_backend" in out

    def test_idempotent_when_value_already_set(self, wrapper):
        src = 'general =\n{\n\toutput_backend = "pa";\n};\n'
        out = wrapper._libconfig_upsert(src, "general", "output_backend", "pa")
        assert out == src

    def test_replaces_existing_value(self, wrapper):
        src = 'general =\n{\n\toutput_backend = "alsa";\n};\n'
        out = wrapper._libconfig_upsert(src, "general", "output_backend", "pa")
        assert 'output_backend = "pa"' in out
        assert 'output_backend = "alsa"' not in out

    def test_skips_commented_lines_when_replacing(self, wrapper):
        # Commented-out template + active line. Replace must hit the active
        # one and leave the comment alone.
        src = (
            "general =\n"
            "{\n"
            '//\toutput_backend = "default";\n'
            '\toutput_backend = "alsa";\n'
            "};\n"
        )
        out = wrapper._libconfig_upsert(src, "general", "output_backend", "pa")
        assert 'output_backend = "pa"' in out
        # Commented template untouched.
        assert '//\toutput_backend = "default";' in out
        # Original active value gone.
        assert 'output_backend = "alsa"' not in out

    def test_escapes_embedded_quotes_in_value(self, wrapper):
        src = "general =\n{\n};\n"
        out = wrapper._libconfig_upsert(src, "general", "name", 'a"b')
        assert r'name = "a\"b";' in out

    def test_appends_new_section_when_missing(self, wrapper):
        src = 'other =\n{\n\tk = "v";\n};\n'
        out = wrapper._libconfig_upsert(src, "general", "output_backend", "pa")
        assert "general =" in out
        assert 'output_backend = "pa"' in out
        # Original other-section content is preserved.
        assert 'k = "v";' in out

    def test_rejects_bad_section_name(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._libconfig_upsert("", "../etc/passwd", "x", "y")

    def test_rejects_bad_key_name(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._libconfig_upsert("general =\n{\n};\n", "general", "bad key", "y")


# ── _render_dropin ──────────────────────────────────────────────────────


class TestRenderDropin:
    def test_minimal_dropin_carries_managed_by_marker(self, wrapper):
        out = wrapper._render_dropin(
            {"service": "shairport-sync", "run_as": "pi"},
            package="music",
        )
        assert "# managed-by: jarvis-package music\n" in out
        assert "[Service]" in out
        assert "User=pi" in out

    def test_full_shairport_drop_in_matches_what_we_did_by_hand(self, wrapper):
        payload = {
            "service": "shairport-sync",
            "run_as": "pi",
            "group": "audio",
            "environment": {
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "PULSE_RUNTIME_PATH": "/run/user/1000/pulse",
            },
            "wants": ["user@1000.service"],
            "after": ["user@1000.service"],
            "restart": "on-failure",
            "restart_sec": 5,
        }
        out = wrapper._render_dropin(payload, package="music")
        # Marker first.
        assert out.splitlines()[0] == "# managed-by: jarvis-package music"
        # [Unit] block.
        assert "[Unit]" in out
        assert "Wants=user@1000.service" in out
        assert "After=user@1000.service" in out
        # [Service] block.
        assert "[Service]" in out
        assert "User=pi" in out
        assert "Group=audio" in out
        assert 'Environment="XDG_RUNTIME_DIR=/run/user/1000"' in out
        assert 'Environment="PULSE_RUNTIME_PATH=/run/user/1000/pulse"' in out
        assert "Restart=on-failure" in out
        assert "RestartSec=5" in out

    def test_invalid_run_as_rejected(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._render_dropin(
                {"run_as": "evil; rm -rf /"}, package="music",
            )

    def test_invalid_env_key_rejected(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._render_dropin(
                {"environment": {"K=Y": "v"}}, package="music",
            )

    def test_invalid_restart_rejected(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._render_dropin(
                {"restart": "$(rm -rf /)"}, package="music",
            )


# ── _normalize_target_file ──────────────────────────────────────────────


class TestNormalizeTargetFile:
    def test_accepts_simple_absolute_path(self, wrapper):
        p = wrapper._normalize_target_file("/etc/shairport-sync.conf")
        assert str(p) == "/etc/shairport-sync.conf"

    def test_rejects_relative_path(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._normalize_target_file("etc/shairport-sync.conf")

    def test_rejects_dot_dot_path(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._normalize_target_file("/etc/../etc/passwd")

    def test_rejects_non_string(self, wrapper):
        with pytest.raises(SystemExit):
            wrapper._normalize_target_file(42)


# ── _validate_package_name / _validate_service_name ─────────────────────


class TestNameValidators:
    def test_valid_package_names_accepted(self, wrapper):
        for name in ("music", "jarvis-cmd-spotify", "abc_123", "x.y"):
            assert wrapper._validate_package_name(name) == name

    @pytest.mark.parametrize("bad", [
        "", "with space", "evil;rm", "../etc/passwd", "x$(y)", None, 42,
    ])
    def test_invalid_package_names_rejected(self, wrapper, bad):
        with pytest.raises(SystemExit):
            wrapper._validate_package_name(bad)

    def test_valid_service_names_accepted(self, wrapper):
        # The `service:` field on a configure_systemd_service op names the
        # base unit being configured. Template instances (e.g. user@1000)
        # would never be configured by a community package — they're for
        # use in Wants=/After= references inside the drop-in.
        for name in ("shairport-sync", "spotifyd", "abc_123", "x.y"):
            assert wrapper._validate_service_name(name) == name

    @pytest.mark.parametrize("bad", [
        "", "with space", "evil;rm", "../etc/passwd", "x$(y)", None, 42,
    ])
    def test_invalid_service_names_rejected(self, wrapper, bad):
        with pytest.raises(SystemExit):
            wrapper._validate_service_name(bad)


# ── remove-managed-dropins ──────────────────────────────────────────────


class TestRemoveManagedDropins:
    """The remove op walks /etc/systemd/system looking for drop-ins whose
    first line is `# managed-by: jarvis-package <name>`. We can't test
    against real /etc, but we can verify the marker matching logic by
    pointing the function at a tmp tree via monkeypatching."""

    def _build_tree(self, root, package_name: str):
        """Build a tmp tree mirroring /etc/systemd/system layout."""
        ours_dir = root / "shairport-sync.service.d"
        ours_dir.mkdir(parents=True)
        ours = ours_dir / "jarvis.conf"
        ours.write_text(
            f"# managed-by: jarvis-package {package_name}\n[Service]\nUser=pi\n"
        )

        theirs_dir = root / "shairport-sync.service.d"
        theirs = theirs_dir / "other.conf"
        theirs.write_text("[Service]\nEnvironment=FOO=bar\n")

        other_pkg_dir = root / "spotifyd.service.d"
        other_pkg_dir.mkdir(parents=True)
        other_pkg = other_pkg_dir / "jarvis.conf"
        other_pkg.write_text(
            "# managed-by: jarvis-package other-package\n[Service]\nUser=spotify\n"
        )
        return ours, theirs, other_pkg

    def test_removes_only_matching_marker(self, wrapper, tmp_path, monkeypatch):
        ours, theirs, other_pkg = self._build_tree(tmp_path, "music")

        # Patch /etc/systemd/system → tmp tree, stub systemctl to a no-op.
        monkeypatch.setattr(wrapper, "Path", wrapper.Path)
        monkeypatch.setattr(wrapper, "_systemctl", lambda *a: None)
        # Replace the hardcoded path inside _op_remove_managed_dropins by
        # monkeypatching the Path constructor for that single string. Use
        # a wrapper-internal helper instead — patch the module-level name.
        original = wrapper.Path
        def _fake_path(p):
            if p == "/etc/systemd/system":
                return tmp_path
            return original(p)
        monkeypatch.setattr(wrapper, "Path", _fake_path)

        wrapper._op_remove_managed_dropins("music")

        assert not ours.exists(), "should remove our drop-in"
        assert theirs.exists(), "should leave unmarked drop-ins alone"
        assert other_pkg.exists(), "should leave other-package's drop-in alone"

    def test_noop_when_no_matching_dropins(self, wrapper, tmp_path, monkeypatch):
        # Empty tree — should not raise.
        monkeypatch.setattr(wrapper, "_systemctl", lambda *a: None)
        original = wrapper.Path
        monkeypatch.setattr(
            wrapper, "Path",
            lambda p: tmp_path if p == "/etc/systemd/system" else original(p),
        )
        wrapper._op_remove_managed_dropins("music")  # no exception = pass


# ── _op_configure_systemd_service ───────────────────────────────────────


class TestConfigureSystemdService:
    """End-to-end op tests — monkeypatch systemctl + the path constant so we
    can exercise the install flow (write drop-in, daemon-reload, enable,
    restart) without touching the real /etc/systemd or running systemctl.

    The `enable` flag was added to fix the audacy install gap (mpd dies on
    reboot because the unit was never enabled — see
    prds/audacy-mpd-install-gap.md)."""

    def _patch(self, wrapper, tmp_path, monkeypatch, *, is_enabled: bool = False):
        """Common setup: redirect /etc/systemd/system into tmp_path, record
        systemctl invocations, stub the is-enabled probe."""
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(wrapper, "_systemctl", lambda *a: calls.append(a))
        monkeypatch.setattr(wrapper, "_unit_is_enabled", lambda _s: is_enabled)
        original_path = wrapper.Path
        monkeypatch.setattr(
            wrapper, "Path",
            lambda p: tmp_path if p == "/etc/systemd/system" else original_path(p),
        )
        return calls

    def test_enable_true_invokes_systemctl_enable_before_restart(
        self, wrapper, tmp_path, monkeypatch,
    ):
        calls = self._patch(wrapper, tmp_path, monkeypatch)
        wrapper._op_configure_systemd_service(
            {"service": "mpd", "run_as": "pi", "enable": True}, package="audacy",
        )
        assert ("daemon-reload",) in calls
        assert ("enable", "mpd") in calls
        assert ("restart", "mpd") in calls
        # Order matters: daemon-reload, then enable, then restart.
        assert calls.index(("daemon-reload",)) < calls.index(("enable", "mpd"))
        assert calls.index(("enable", "mpd")) < calls.index(("restart", "mpd"))

    def test_enable_false_does_not_invoke_enable(
        self, wrapper, tmp_path, monkeypatch,
    ):
        calls = self._patch(wrapper, tmp_path, monkeypatch)
        wrapper._op_configure_systemd_service(
            {"service": "mpd", "run_as": "pi", "enable": False}, package="audacy",
        )
        assert ("enable", "mpd") not in calls
        assert ("restart", "mpd") in calls

    def test_enable_omitted_does_not_invoke_enable(
        self, wrapper, tmp_path, monkeypatch,
    ):
        # Backwards-compat: older manifests with no `enable` key behave
        # exactly as before — drop-in + restart, never enable.
        calls = self._patch(wrapper, tmp_path, monkeypatch)
        wrapper._op_configure_systemd_service(
            {"service": "shairport-sync", "run_as": "pi"}, package="music",
        )
        assert not any(c[0] == "enable" for c in calls)

    def test_enable_non_bool_rejected(self, wrapper, tmp_path, monkeypatch):
        self._patch(wrapper, tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            wrapper._op_configure_systemd_service(
                {"service": "mpd", "enable": "yes"}, package="audacy",
            )

    def test_idempotent_when_dropin_matches_and_unit_already_enabled(
        self, wrapper, tmp_path, monkeypatch,
    ):
        # First run writes the drop-in + calls enable + restart.
        calls = self._patch(wrapper, tmp_path, monkeypatch, is_enabled=False)
        payload = {"service": "mpd", "run_as": "pi", "enable": True}
        wrapper._op_configure_systemd_service(payload, package="audacy")
        assert ("enable", "mpd") in calls

        # Second run: drop-in already present, unit reports enabled — full
        # no-op. Re-patch with is_enabled=True to model post-first-install.
        calls.clear()
        monkeypatch.setattr(wrapper, "_unit_is_enabled", lambda _s: True)
        wrapper._op_configure_systemd_service(payload, package="audacy")
        assert calls == [], "second run should be a clean no-op"

    def test_dropin_matches_but_unit_not_enabled_still_runs_enable(
        self, wrapper, tmp_path, monkeypatch,
    ):
        # Drop-in already on disk from a prior install, but the user (or a
        # previous wrapper version) never ran systemctl enable. Re-running
        # the op must enable the unit even though the file is unchanged —
        # this is exactly the audacy-mpd-install-gap recovery path.
        dropin_dir = tmp_path / "mpd.service.d"
        dropin_dir.mkdir()
        payload = {"service": "mpd", "run_as": "pi", "enable": True}
        rendered = wrapper._render_dropin(payload, package="audacy")
        (dropin_dir / "jarvis.conf").write_text(rendered)

        calls = self._patch(wrapper, tmp_path, monkeypatch, is_enabled=False)
        wrapper._op_configure_systemd_service(payload, package="audacy")
        assert ("enable", "mpd") in calls
        assert ("restart", "mpd") in calls
