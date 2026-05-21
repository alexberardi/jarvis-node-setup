"""Tests for PiBluetoothProvider.set_discoverable ordering.

Phones (iOS in particular) filter the Bluetooth picker by pairable+discoverable
state, so the order of bluetoothctl commands matters. These tests pin down the
ordering so a future refactor can't silently drop ``pairable on`` or reorder
``discoverable-timeout`` past ``discoverable on``.
"""

import subprocess
from unittest.mock import patch

from core.platform_abstraction import PiBluetoothProvider


def _fake_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestSetDiscoverable:

    def test_enable_runs_setup_commands_in_required_order(self):
        provider = PiBluetoothProvider()
        show_output = "\n".join([
            "Powered: yes",
            "Discoverable: yes",
            "Pairable: yes",
            "Alias: jarvis-dev",
        ])

        def side_effect(*args, **_):
            if args == ("show",):
                return _fake_run(stdout=show_output)
            return _fake_run()

        with patch.object(provider, "_run_bluetoothctl", side_effect=side_effect) as mock_run:
            assert provider.set_discoverable(enabled=True, timeout=120) is True

        positional = [c.args for c in mock_run.call_args_list]
        assert positional[0] == ("power", "on")
        assert positional[1] == ("pairable", "on")
        assert positional[2] == ("discoverable-timeout", "120")
        assert positional[3] == ("discoverable", "on")
        # `show` is the diagnostic dump and must come after the toggle so it
        # reflects the post-enable adapter state.
        assert positional[4] == ("show",)

    def test_disable_does_not_touch_pairable_or_power(self):
        provider = PiBluetoothProvider()
        with patch.object(provider, "_run_bluetoothctl", return_value=_fake_run()) as mock_run:
            assert provider.set_discoverable(enabled=False) is True

        positional = [c.args for c in mock_run.call_args_list]
        assert positional == [("discoverable", "off")]

    def test_returns_false_when_discoverable_command_fails(self):
        provider = PiBluetoothProvider()

        def side_effect(*args, **_):
            if args[0] == "discoverable":
                return _fake_run(returncode=1, stderr="adapter not ready")
            return _fake_run()

        with patch.object(provider, "_run_bluetoothctl", side_effect=side_effect):
            assert provider.set_discoverable(enabled=True, timeout=120) is False

    def test_returns_false_on_subprocess_error(self):
        provider = PiBluetoothProvider()
        with patch.object(
            provider,
            "_run_bluetoothctl",
            side_effect=FileNotFoundError("bluetoothctl missing"),
        ):
            assert provider.set_discoverable(enabled=True, timeout=120) is False
