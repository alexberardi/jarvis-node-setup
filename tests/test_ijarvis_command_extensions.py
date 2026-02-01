"""
Unit tests for IJarvisCommand extensions: init_data() and required_packages.

Tests the new optional methods added to the command interface
for package dependencies and initialization hooks.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_package import JarvisPackage
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


class MinimalCommand(IJarvisCommand):
    """Minimal command implementation for testing defaults"""

    @property
    def command_name(self) -> str:
        return "test_minimal"

    @property
    def description(self) -> str:
        return "A minimal test command"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["test"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [CommandExample("test command", {}, is_primary=True)]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [CommandExample("test command", {})]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        return CommandResponse.success_response(context_data={"test": True})


class CommandWithPackages(MinimalCommand):
    """Command that declares package dependencies"""

    @property
    def command_name(self) -> str:
        return "test_with_packages"

    @property
    def required_packages(self) -> List[JarvisPackage]:
        return [
            JarvisPackage("music-assistant-client", "1.0.0"),
            JarvisPackage("httpx", ">=0.25.0"),
        ]


class CommandWithInitData(MinimalCommand):
    """Command that implements init_data()"""

    @property
    def command_name(self) -> str:
        return "test_with_init"

    def init_data(self) -> Dict[str, Any]:
        return {"status": "success", "devices_synced": 3}


class CommandWithBoth(MinimalCommand):
    """Command with both packages and init_data"""

    @property
    def command_name(self) -> str:
        return "test_with_both"

    @property
    def required_packages(self) -> List[JarvisPackage]:
        return [JarvisPackage("some-package", "2.0.0")]

    def init_data(self) -> Dict[str, Any]:
        return {"initialized": True}


class TestRequiredPackagesDefault:
    """Test default required_packages behavior"""

    def test_default_returns_empty_list(self):
        """Commands without required_packages return empty list"""
        cmd = MinimalCommand()
        assert cmd.required_packages == []

    def test_default_is_list_type(self):
        """Default required_packages is a list"""
        cmd = MinimalCommand()
        assert isinstance(cmd.required_packages, list)


class TestRequiredPackagesCustom:
    """Test custom required_packages implementation"""

    def test_returns_package_list(self):
        """Command can return list of JarvisPackage"""
        cmd = CommandWithPackages()
        packages = cmd.required_packages

        assert len(packages) == 2
        assert all(isinstance(p, JarvisPackage) for p in packages)

    def test_package_names(self):
        """Package names are accessible"""
        cmd = CommandWithPackages()
        names = [p.name for p in cmd.required_packages]

        assert "music-assistant-client" in names
        assert "httpx" in names

    def test_package_versions(self):
        """Package versions are accessible"""
        cmd = CommandWithPackages()
        packages = {p.name: p.version for p in cmd.required_packages}

        assert packages["music-assistant-client"] == "1.0.0"
        assert packages["httpx"] == ">=0.25.0"


class TestInitDataDefault:
    """Test default init_data behavior"""

    def test_default_returns_no_init_required(self):
        """Commands without init_data return default status"""
        cmd = MinimalCommand()
        result = cmd.init_data()

        assert result == {"status": "no_init_required"}

    def test_default_returns_dict(self):
        """Default init_data returns a dictionary"""
        cmd = MinimalCommand()
        result = cmd.init_data()

        assert isinstance(result, dict)


class TestInitDataCustom:
    """Test custom init_data implementation"""

    def test_custom_init_data_called(self):
        """Custom init_data returns command-specific result"""
        cmd = CommandWithInitData()
        result = cmd.init_data()

        assert result["status"] == "success"
        assert result["devices_synced"] == 3

    def test_returns_dict_type(self):
        """init_data returns a dictionary"""
        cmd = CommandWithInitData()
        result = cmd.init_data()

        assert isinstance(result, dict)


class TestCombinedFunctionality:
    """Test commands with both packages and init_data"""

    def test_can_have_both(self):
        """Command can implement both required_packages and init_data"""
        cmd = CommandWithBoth()

        packages = cmd.required_packages
        init_result = cmd.init_data()

        assert len(packages) == 1
        assert packages[0].name == "some-package"
        assert init_result["initialized"] is True

    def test_existing_functionality_preserved(self):
        """New methods don't break existing command functionality"""
        cmd = CommandWithBoth()

        # Existing properties still work
        assert cmd.command_name == "test_with_both"
        assert cmd.description == "A minimal test command"
        assert cmd.keywords == ["test"]

        # Run still works
        mock_request = MagicMock(spec=RequestInformation)
        response = cmd.run(mock_request)
        assert response.success is True
