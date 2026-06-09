"""Unit tests for the maintenance-restart scheduler.

The scheduler reads settings and triggers ``os._exit(0)`` at a specific
local time or when RSS crosses a configured ceiling. We exercise the
decision logic by patching ``_exit_for_restart`` to record calls, and
``utils.config_service.Config`` getters so each test pins the exact
settings under test.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import pytest

from services.maintenance_restart_service import (
    MaintenanceRestartService,
    _parse_hhmm,
    _write_maintenance_restart_marker,
    clear_pending_maintenance_restart,
    has_pending_maintenance_restart,
)
import services.maintenance_restart_service as mrs


# ── _parse_hhmm ──────────────────────────────────────────────────────────


class TestParseHHMM:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("03:00", (3, 0)),
            ("00:00", (0, 0)),
            ("23:59", (23, 59)),
            ("12:30", (12, 30)),
            (" 03:00 ", (3, 0)),  # whitespace tolerated
        ],
    )
    def test_valid_strings(self, s: str, expected: tuple[int, int]) -> None:
        assert _parse_hhmm(s) == expected

    @pytest.mark.parametrize(
        "s",
        [
            "24:00",       # hour out of range
            "23:60",       # minute out of range
            "-1:00",       # negative
            "03:00:00",    # too many parts
            "0300",        # missing colon
            "abc:00",      # non-numeric
            "",            # empty
        ],
    )
    def test_invalid_strings_return_none(self, s: str) -> None:
        assert _parse_hhmm(s) is None

    def test_non_string_returns_none(self) -> None:
        assert _parse_hhmm(None) is None  # type: ignore[arg-type]
        assert _parse_hhmm(300) is None  # type: ignore[arg-type]


# ── Scheduler iteration ──────────────────────────────────────────────────


class _RecordExit(Exception):
    """Sentinel raised by patched _exit_for_restart so the test can assert
    it would have exited without actually killing the test process."""


def _patch_exit(svc: MaintenanceRestartService) -> list[str]:
    """Replace ``_exit_for_restart`` with a recorder. Returns the list it
    appends to (one entry per call, value is the reason)."""
    reasons: list[str] = []

    def _record(*, reason: str) -> None:
        reasons.append(reason)
        raise _RecordExit(reason)

    svc._exit_for_restart = _record  # type: ignore[method-assign]
    return reasons


class TestSchedulerIteration:
    def test_disabled_short_circuits(self) -> None:
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg:
            mock_cfg.get_bool.return_value = False  # disabled
            svc._check_once()
        assert reasons == []
        assert mock_cfg.get_bool.called

    def test_rss_above_ceiling_triggers_emergency_exit(self) -> None:
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=500,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            with pytest.raises(_RecordExit):
                svc._check_once()
        assert reasons == ["rss_ceiling"]

    def test_rss_below_ceiling_does_not_trigger(self) -> None:
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=120,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            # Time set to something distant from "now" so the scheduled
            # check also doesn't fire.
            mock_cfg.get_str.return_value = "03:00"
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 14, 30,
                )
                svc._check_once()
        assert reasons == []

    def test_rss_ceiling_zero_disables_check(self) -> None:
        """A ceiling of 0 (or negative) means "no RSS-based trigger" — only
        the scheduled time can restart."""
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=99999,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 0  # disabled
            mock_cfg.get_str.return_value = "03:00"
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 14, 30,
                )
                svc._check_once()
        assert reasons == []

    def test_scheduled_time_match_triggers(self) -> None:
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=100,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            mock_cfg.get_str.return_value = "03:00"
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 3, 0,
                )
                with pytest.raises(_RecordExit):
                    svc._check_once()
        assert reasons == ["scheduled"]

    def test_scheduled_time_only_fires_once_per_day(self) -> None:
        """Once we've stamped today's date in ``_last_triggered_date``,
        subsequent calls at the same minute must no-op."""
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        svc._last_triggered_date = datetime.date(2026, 6, 9)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=100,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            mock_cfg.get_str.return_value = "03:00"
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 3, 0,
                )
                svc._check_once()
        assert reasons == []

    def test_invalid_time_falls_back_to_default(self) -> None:
        """A malformed setting must default to 03:00 — the scheduler
        never silently disables itself just because the user typed a
        bad time."""
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=100,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            mock_cfg.get_str.return_value = "garbage"  # malformed
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 3, 0,  # 03:00 — matches the default
                )
                with pytest.raises(_RecordExit):
                    svc._check_once()
        assert reasons == ["scheduled"]

    def test_scheduled_no_match_when_minute_differs(self) -> None:
        svc = MaintenanceRestartService()
        reasons = _patch_exit(svc)
        with patch("services.maintenance_restart_service.Config") as mock_cfg, \
             patch(
                 "services.maintenance_restart_service._read_rss_mb",
                 return_value=100,
             ):
            mock_cfg.get_bool.return_value = True
            mock_cfg.get_int.return_value = 320
            mock_cfg.get_str.return_value = "03:00"
            with patch(
                "services.maintenance_restart_service.datetime.datetime",
                wraps=datetime.datetime,
            ) as mock_dt_cls:
                # 03:01, off by one minute — no fire.
                mock_dt_cls.now = lambda: datetime.datetime(
                    2026, 6, 9, 3, 1,
                )
                svc._check_once()
        assert reasons == []


# ── Silent-restart marker ────────────────────────────────────────────────


class TestMaintenanceMarker:
    """The marker file is the contract between the scheduler and main.py's
    boot-time warmup gate. Without it, a 3 AM restart would loudly speak
    the LLM warmup response through the speaker — exactly what the daily
    restart is meant to avoid."""

    def test_write_then_read_then_clear(self, tmp_path) -> None:
        marker = tmp_path / "marker"
        with patch.object(mrs, "_MAINTENANCE_RESTART_MARKER", marker):
            assert has_pending_maintenance_restart() is False
            _write_maintenance_restart_marker("scheduled")
            assert has_pending_maintenance_restart() is True
            clear_pending_maintenance_restart()
            assert has_pending_maintenance_restart() is False

    def test_clear_is_idempotent_when_marker_absent(self, tmp_path) -> None:
        # Calling clear on a fresh boot (no marker present) must NOT raise.
        # Without this guard, main.py's startup code would have to wrap
        # every call in a try/except.
        marker = tmp_path / "absent"
        with patch.object(mrs, "_MAINTENANCE_RESTART_MARKER", marker):
            clear_pending_maintenance_restart()  # must not raise
            assert has_pending_maintenance_restart() is False

    def test_write_creates_parent_directory(self, tmp_path) -> None:
        # ~/.jarvis may not exist on a freshly-provisioned node before
        # any other component creates it. The marker writer must handle
        # that — same convention the install handler uses.
        marker = tmp_path / "nested" / "deep" / "marker"
        with patch.object(mrs, "_MAINTENANCE_RESTART_MARKER", marker):
            _write_maintenance_restart_marker("rss_ceiling")
            assert marker.exists()
            assert has_pending_maintenance_restart() is True

    def test_exit_writes_marker_before_sleep(self, tmp_path) -> None:
        # _exit_for_restart calls os._exit which would kill the test
        # process; patch it to a recorder. The marker must be written
        # BEFORE the sleep so an aggressive TimeoutStopSec can't take
        # us out before the signal is durable on disk.
        marker = tmp_path / "marker"
        svc = MaintenanceRestartService()
        exit_calls: list[int] = []

        def _fake_exit(_code: int) -> None:
            exit_calls.append(_code)

        with patch.object(mrs, "_MAINTENANCE_RESTART_MARKER", marker), \
             patch.object(mrs.os, "_exit", _fake_exit), \
             patch.object(mrs.time, "sleep", lambda _s: None):
            svc._exit_for_restart(reason="scheduled")

        assert marker.exists()
        assert exit_calls == [0]
