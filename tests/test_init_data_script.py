"""
Unit tests for scripts/init_data.py runner script.

Tests the command initialization runner that calls init_data()
on commands for first-install setup.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


class MockCommandWithInitData(IJarvisCommand):
    """Mock command that implements init_data"""

    @property
    def command_name(self) -> str:
        return "mock_with_init"

    @property
    def description(self) -> str:
        return "Mock command with init_data"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["mock"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [CommandExample("test", {}, is_primary=True)]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [CommandExample("test", {})]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        return CommandResponse.success_response(context_data={})

    def init_data(self) -> Dict[str, Any]:
        return {"status": "success", "devices_synced": 5}


class MockCommandWithoutInitData(IJarvisCommand):
    """Mock command that uses default init_data"""

    @property
    def command_name(self) -> str:
        return "mock_no_init"

    @property
    def description(self) -> str:
        return "Mock command without custom init_data"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["mock"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [CommandExample("test", {}, is_primary=True)]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [CommandExample("test", {})]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        return CommandResponse.success_response(context_data={})


class TestInitDataRunner:
    """Test the init_data runner logic"""

    def test_run_init_data_with_custom_implementation(self):
        """Command with custom init_data returns its result"""
        from scripts.init_data import run_init_data

        cmd = MockCommandWithInitData()
        result = run_init_data(cmd)

        assert result["status"] == "success"
        assert result["devices_synced"] == 5

    def test_run_init_data_with_default_implementation(self):
        """Command without custom init_data returns default status"""
        from scripts.init_data import run_init_data

        cmd = MockCommandWithoutInitData()
        result = run_init_data(cmd)

        assert result["status"] == "no_init_required"


class TestFindCommand:
    """Test command lookup by name"""

    def test_find_existing_command(self):
        """Can find a command by name"""
        from scripts.init_data import find_command

        mock_commands = {
            "mock_with_init": MockCommandWithInitData(),
            "mock_no_init": MockCommandWithoutInitData(),
        }

        with patch("scripts.init_data.get_all_commands", return_value=mock_commands):
            cmd = find_command("mock_with_init")
            assert cmd is not None
            assert cmd.command_name == "mock_with_init"

    def test_find_nonexistent_command(self):
        """Returns None for unknown command"""
        from scripts.init_data import find_command

        mock_commands = {
            "mock_with_init": MockCommandWithInitData(),
        }

        with patch("scripts.init_data.get_all_commands", return_value=mock_commands):
            cmd = find_command("nonexistent")
            assert cmd is None


class TestMainFunction:
    """Test the main CLI function"""

    def test_main_with_valid_command(self, capsys):
        """Main runs init_data for valid command"""
        from scripts.init_data import main

        mock_commands = {
            "mock_with_init": MockCommandWithInitData(),
        }

        with patch("scripts.init_data.get_all_commands", return_value=mock_commands):
            exit_code = main(["--command", "mock_with_init"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "success" in captured.out.lower()

    def test_main_with_invalid_command(self, capsys):
        """Main returns error for unknown command"""
        from scripts.init_data import main

        mock_commands = {}

        with patch("scripts.init_data.get_all_commands", return_value=mock_commands):
            exit_code = main(["--command", "nonexistent"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_main_with_no_init_required(self, capsys):
        """Main handles commands with default init_data"""
        from scripts.init_data import main

        mock_commands = {
            "mock_no_init": MockCommandWithoutInitData(),
        }

        with patch("scripts.init_data.get_all_commands", return_value=mock_commands):
            exit_code = main(["--command", "mock_no_init"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no_init_required" in captured.out.lower()

    def test_main_requires_command_arg(self, capsys):
        """Main returns error if --command not provided"""
        from scripts.init_data import main

        with pytest.raises(SystemExit) as exc_info:
            main([])

        # argparse exits with code 2 for missing required args
        assert exc_info.value.code == 2
