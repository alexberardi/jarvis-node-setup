"""Tests for scripts/jarvis-post-install-verify (Layer 2 post-install watchdog).

Exercises the watchdog's decision logic by stubbing `systemctl` via $PATH and
driving the script against scripted state. The stub reads a JSON state file
in $STUB_STATE_DIR and increments a per-`show`-call counter so multi-state
scenarios (activating → active, crash-loop tick) can be expressed as a list
of dicts, one entry per poll iteration.

Coverage:
  - No marker file → silent no-op
  - Healthy service → marker cleared, no rollback
  - Service `failed` + `start-limit-hit` + backup → rollback, forensics preserved
  - Stays `activating` past HEALTH_TIMEOUT → rollback
  - `active` but NRestarts ticking (crash-loop) → rollback
  - No backup → "no_backup_available" log, install dir untouched, exit 1
  - Malformed marker → still processes

These hit every branch of the rollback path without needing a real systemd or
real `mv` on /opt — INSTALL_DIR is pointed at a tmp dir per test.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = _REPO_ROOT / "scripts" / "jarvis-post-install-verify"


# A systemctl stub that:
#   - Logs every invocation to $STUB_STATE_DIR/calls.log (one argv per line)
#   - For `systemctl show -p <prop> --value <unit>`: reads $STUB_STATE_DIR/
#     state.json. If it's a dict, returns the same value every call (constant
#     state). If it's a list, returns entries by *poll iteration* — the
#     watchdog calls show 3 times per iteration (ActiveState, NRestarts,
#     Result), so the stub groups every 3 show-calls into one tick. Last entry
#     sticks once the list is exhausted.
#   - All other invocations (stop/start/reset-failed) exit 0 without effect.
_SYSTEMCTL_STUB = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

state_dir = Path(os.environ["STUB_STATE_DIR"])
with (state_dir / "calls.log").open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")

if len(sys.argv) >= 4 and sys.argv[1] == "show":
    prop = sys.argv[3]
    state_path = state_dir / "state.json"
    counter_path = state_dir / "counter.txt"
    counter = int(counter_path.read_text()) if counter_path.exists() else 0
    counter += 1
    counter_path.write_text(str(counter))

    if not state_path.exists():
        print("unknown")
        sys.exit(0)
    state = json.loads(state_path.read_text())

    # The watchdog issues three show-calls per poll iteration; group them.
    tick = (counter - 1) // 3

    if isinstance(state, list):
        idx = min(tick, len(state) - 1)
        print(state[idx].get(prop, ""))
    else:
        print(state.get(prop, ""))
    sys.exit(0)

# stop / start / reset-failed / etc. — no-op
sys.exit(0)
"""


@pytest.fixture
def env(tmp_path):
    """Build a fully isolated environment for one watchdog run."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "systemctl").write_text(_SYSTEMCTL_STUB)
    (stub_dir / "systemctl").chmod(0o755)

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    stub_state_dir = tmp_path / "stub"
    stub_state_dir.mkdir()

    # Pre-create the install location root but not the install dir itself
    # (tests opt into _setup_install to create it).
    (tmp_path / "opt").mkdir()

    return {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_STATE_DIR": str(stub_state_dir),
        "STATE_DIR": str(state_dir),
        "INSTALL_DIR": str(tmp_path / "opt" / "jarvis-node"),
        "SERVICE": "jarvis-node.service",
        # Tight timings so the test suite stays fast. Real defaults are
        # 120s / 15s / 3s; the script's logic is identical at any timing.
        "HEALTH_TIMEOUT": "6",
        "MIN_STABLE_S": "2",
        "POLL_INTERVAL": "1",
    }


def _write_marker(env, target="0.1.110", previous="0.1.108"):
    marker = Path(env["STATE_DIR"]) / "install-pending-verification.json"
    marker.write_text(json.dumps({
        "target_version": target,
        "previous_version": previous,
        "install_timestamp": "2026-06-09T13:00:00-04:00",
    }))


def _write_state(env, state):
    """state is a dict (constant) or list of dicts (scripted, one per tick)."""
    (Path(env["STUB_STATE_DIR"]) / "state.json").write_text(json.dumps(state))


def _setup_install(env, with_backup=True, target_version="0.1.110", backup_version="0.1.108"):
    install = Path(env["INSTALL_DIR"])
    install.mkdir(parents=True, exist_ok=True)
    (install / "VERSION").write_text(target_version)
    (install / "marker-install").write_text("install-sentinel")
    if with_backup:
        backup = install.with_name(install.name + ".bak")
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "VERSION").write_text(backup_version)
        (backup / "marker-backup").write_text("backup-sentinel")


def _run(env, timeout=20):
    return subprocess.run(
        [str(WRAPPER)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ----------------------------------------------------------------------
# 1. No marker → silent no-op
# ----------------------------------------------------------------------

class TestNoMarker:
    def test_no_marker_exits_quietly(self, env):
        result = _run(env)
        assert result.returncode == 0
        assert not (Path(env["STATE_DIR"]) / "last-rollback.json").exists()


# ----------------------------------------------------------------------
# 2. Healthy service → clear marker, no rollback
# ----------------------------------------------------------------------

class TestHealthyService:
    def test_active_from_start_clears_marker(self, env):
        _write_marker(env)
        _setup_install(env, with_backup=True)
        _write_state(env, {
            "ActiveState": "active", "NRestarts": "0", "Result": "success"
        })

        result = _run(env)
        assert result.returncode == 0, result.stderr

        # Marker gone, no rollback log, install unchanged.
        marker = Path(env["STATE_DIR"]) / "install-pending-verification.json"
        assert not marker.exists()
        assert not (Path(env["STATE_DIR"]) / "last-rollback.json").exists()

        install = Path(env["INSTALL_DIR"])
        assert (install / "marker-install").exists(), "install dir must be untouched"
        # The backup is still adjacent — only a rollback consumes it.
        assert install.with_name(install.name + ".bak").exists()

    def test_activating_then_active_succeeds(self, env):
        _write_marker(env)
        _setup_install(env, with_backup=True)
        _write_state(env, [
            {"ActiveState": "activating", "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active",     "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active",     "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active",     "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active",     "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active",     "NRestarts": "0", "Result": "success"},
        ])

        result = _run(env)
        assert result.returncode == 0, result.stderr
        assert not (Path(env["STATE_DIR"]) / "install-pending-verification.json").exists()


# ----------------------------------------------------------------------
# 3. Failed service + backup → rollback to .bak
# ----------------------------------------------------------------------

class TestFailedServiceWithBackup:
    def test_start_limit_hit_triggers_rollback(self, env):
        _write_marker(env, target="0.1.110", previous="0.1.108")
        _setup_install(env, with_backup=True)
        _write_state(env, {
            "ActiveState": "failed", "NRestarts": "10", "Result": "start-limit-hit"
        })

        result = _run(env)
        assert result.returncode == 0, result.stderr

        # Marker cleared, rollback log written
        marker = Path(env["STATE_DIR"]) / "install-pending-verification.json"
        assert not marker.exists()

        log_path = Path(env["STATE_DIR"]) / "last-rollback.json"
        assert log_path.exists()
        log = json.loads(log_path.read_text())
        assert log["rolled_back"] is True
        assert log["reason"] == "service_unhealthy_after_install"
        assert log["from_version"] == "0.1.110"
        assert log["to_version"] == "0.1.108"

        # Broken install preserved at .broken-<target>
        broken = Path(env["INSTALL_DIR"] + ".broken-0.1.110")
        assert broken.is_dir(), "broken install dir not preserved"
        assert (broken / "marker-install").exists()

        # The .bak is now the live install
        install = Path(env["INSTALL_DIR"])
        assert install.is_dir()
        assert (install / "marker-backup").exists()
        assert (install / "VERSION").read_text() == "0.1.108"

        # The .bak no longer exists — it's been moved into place
        assert not install.with_name(install.name + ".bak").exists()

    def test_never_healthy_within_timeout_triggers_rollback(self, env):
        # Stays 'activating' the whole HEALTH_TIMEOUT window.
        _write_marker(env)
        _setup_install(env, with_backup=True)
        _write_state(env, {
            "ActiveState": "activating", "NRestarts": "0", "Result": "success"
        })

        result = _run(env)
        assert result.returncode == 0, result.stderr

        log = json.loads((Path(env["STATE_DIR"]) / "last-rollback.json").read_text())
        assert log["rolled_back"] is True


# ----------------------------------------------------------------------
# 4. No backup → can't roll back; failure log + exit 1
# ----------------------------------------------------------------------

class TestNoBackup:
    def test_no_backup_writes_failure_log(self, env):
        _write_marker(env)
        _setup_install(env, with_backup=False)
        _write_state(env, {
            "ActiveState": "failed", "NRestarts": "10", "Result": "start-limit-hit"
        })

        result = _run(env)
        assert result.returncode == 1, (
            f"watchdog should exit 1 when it can't roll back. stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

        # Marker still cleared (don't loop on boots)
        assert not (Path(env["STATE_DIR"]) / "install-pending-verification.json").exists()

        # Failure log written
        log = json.loads((Path(env["STATE_DIR"]) / "last-rollback.json").read_text())
        assert log["rolled_back"] is False
        assert log["reason"] == "no_backup_available"

        # Install dir untouched — never move when we can't recover
        install = Path(env["INSTALL_DIR"])
        assert install.is_dir()
        assert (install / "marker-install").exists()
        assert not Path(env["INSTALL_DIR"] + ".broken-0.1.110").exists(), (
            "must not move install to .broken-* when there's nothing to roll back to"
        )


# ----------------------------------------------------------------------
# 5. Crash-loop detection (NRestarts ticks)
# ----------------------------------------------------------------------

class TestCrashLoopDetection:
    def test_active_with_ticking_restarts_eventually_rolls_back(self, env):
        # Baseline NRestarts=0 (first show before the loop). Then every poll
        # iteration shows `active` BUT with NRestarts ticking up. The script
        # treats this as a stability break (systemd respawned us between
        # polls) and never reaches the steady-state window.
        _write_marker(env)
        _setup_install(env, with_backup=True)
        _write_state(env, [
            {"ActiveState": "active", "NRestarts": "0", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "1", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "2", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "3", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "4", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "5", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "6", "Result": "success"},
            {"ActiveState": "active", "NRestarts": "7", "Result": "success"},
        ])

        result = _run(env)
        assert result.returncode == 0, result.stderr

        log = json.loads((Path(env["STATE_DIR"]) / "last-rollback.json").read_text())
        assert log["rolled_back"] is True


# ----------------------------------------------------------------------
# 6. Malformed marker — still processes gracefully
# ----------------------------------------------------------------------

class TestMalformedMarker:
    def test_garbage_marker_still_processes_healthy(self, env):
        # An unreadable / non-JSON marker should not crash the watchdog —
        # fields default to "unknown" and the run continues normally.
        marker = Path(env["STATE_DIR"]) / "install-pending-verification.json"
        marker.write_text("this is definitely not json {")
        _setup_install(env, with_backup=True)
        _write_state(env, {
            "ActiveState": "active", "NRestarts": "0", "Result": "success"
        })

        result = _run(env)
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
