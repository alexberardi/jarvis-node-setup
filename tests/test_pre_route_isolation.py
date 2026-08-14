"""_safe_pre_route is fully fault-isolated.

The node-side fast-path pre-route loop (try_pre_route / parse_command) runs on
the voice hot path and iterates every command. A single command whose
pre_route() raises (a bad regex, a raising fast_path_patterns/handler, a broken
override) must NOT abort pre-routing for every other command that turn — the
same all-or-nothing failure class as the conversation-start schema bug. It must
also still honor the legacy no-kwarg pre_route() signature.
"""
from unittest.mock import MagicMock

from utils.command_execution_service import CommandExecutionService

_safe = CommandExecutionService._safe_pre_route


def test_raising_pre_route_is_skipped_not_propagated():
    cmd = MagicMock()
    cmd.command_name = "boom"
    cmd.pre_route.side_effect = RuntimeError("bad regex compiled at call time")
    # Returns None (fall through to normal routing), never raises.
    assert _safe(cmd, "turn on the lights", set()) is None


def test_normal_result_passes_through():
    cmd = MagicMock()
    cmd.command_name = "ok"
    cmd.pre_route.return_value = {"matched": True}
    assert _safe(cmd, "hello", set()) == {"matched": True}


def test_legacy_no_kwarg_signature_still_supported():
    # An override predating disabled_pattern_ids: the kwarg call raises the compat
    # TypeError, and _safe_pre_route retries with the legacy signature.
    def pre_route(voice_command, **kwargs):
        if "disabled_pattern_ids" in kwargs:
            raise TypeError(
                "pre_route() got an unexpected keyword argument 'disabled_pattern_ids'"
            )
        return {"legacy": True}

    cmd = MagicMock()
    cmd.command_name = "legacy"
    cmd.pre_route.side_effect = pre_route
    assert _safe(cmd, "hi", set()) == {"legacy": True}


def test_legacy_fallback_that_also_raises_returns_none():
    def pre_route(voice_command, **kwargs):
        if "disabled_pattern_ids" in kwargs:
            raise TypeError("unexpected keyword argument 'disabled_pattern_ids'")
        raise RuntimeError("legacy path also broken")

    cmd = MagicMock()
    cmd.command_name = "legacy_bad"
    cmd.pre_route.side_effect = pre_route
    assert _safe(cmd, "hi", set()) is None
