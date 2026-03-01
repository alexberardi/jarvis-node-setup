"""
Unit tests for the generic parameter validation framework.

Tests ValidationResult, validate_call(), enum validation in IJarvisParameter,
CommandResponse.validation_error(), and ControlDeviceCommand.validate_call().
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from core.validation_result import ValidationResult


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class StubCommand(IJarvisCommand):
    """Minimal command for testing validate_call() default behavior."""

    def __init__(self, params: list[JarvisParameter] | None = None):
        self._params = params or []

    @property
    def command_name(self) -> str:
        return "test_stub"

    @property
    def description(self) -> str:
        return "Stub command for testing"

    @property
    def keywords(self) -> List[str]:
        return ["test"]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return self._params

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [CommandExample(voice_command="test", expected_parameters={})]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        return CommandResponse.success_response(context_data=kwargs)


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

class TestValidationResult:

    def test_success_result(self):
        r = ValidationResult(success=True, param_name="x", command_name="cmd")
        assert r.success is True
        assert r.message is None
        assert r.suggested_value is None
        assert r.valid_values is None

    def test_failure_result(self):
        r = ValidationResult(
            success=False,
            param_name="entity_id",
            command_name="control_device",
            message="Not found",
            valid_values=["light.a", "light.b"],
        )
        assert r.success is False
        assert r.message == "Not found"
        assert r.valid_values == ["light.a", "light.b"]

    def test_auto_correction_result(self):
        r = ValidationResult(
            success=True,
            param_name="entity_id",
            command_name="control_device",
            suggested_value="light.my_office",
        )
        assert r.success is True
        assert r.suggested_value == "light.my_office"


# ---------------------------------------------------------------------------
# IJarvisParameter.validate() — enum_values
# ---------------------------------------------------------------------------

class TestParameterEnumValidation:

    def test_valid_enum_value(self):
        param = JarvisParameter("action", "string", enum_values=["play", "pause", "stop"])
        is_valid, msg = param.validate("play")
        assert is_valid is True
        assert msg is None

    def test_invalid_enum_value(self):
        param = JarvisParameter("action", "string", enum_values=["play", "pause", "stop"])
        is_valid, msg = param.validate("rewind")
        assert is_valid is False
        assert "rewind" in msg
        assert "play" in msg

    def test_no_enum_values_skips_check(self):
        param = JarvisParameter("action", "string")
        is_valid, msg = param.validate("anything_goes")
        assert is_valid is True

    def test_none_value_skips_enum_check(self):
        param = JarvisParameter("action", "string", required=False, enum_values=["a", "b"])
        is_valid, msg = param.validate(None)
        assert is_valid is True

    def test_int_coerced_to_string_for_enum(self):
        param = JarvisParameter("level", "string", enum_values=["1", "2", "3"])
        is_valid, msg = param.validate("2")
        assert is_valid is True

    def test_type_check_runs_before_enum(self):
        param = JarvisParameter("count", "int", enum_values=["1", "2"])
        is_valid, msg = param.validate("not_an_int")
        assert is_valid is False
        assert "type" in msg.lower()


# ---------------------------------------------------------------------------
# JarvisCommandBase.validate_call() — default implementation
# ---------------------------------------------------------------------------

class TestValidateCallDefault:

    def test_no_params_returns_empty(self):
        cmd = StubCommand()
        results = cmd.validate_call()
        assert results == []

    def test_valid_params_returns_empty(self):
        cmd = StubCommand([
            JarvisParameter("name", "string", required=True),
        ])
        results = cmd.validate_call(name="hello")
        assert results == []

    def test_enum_violation_returned(self):
        cmd = StubCommand([
            JarvisParameter("action", "string", required=True, enum_values=["a", "b"]),
        ])
        results = cmd.validate_call(action="c")
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].param_name == "action"
        assert results[0].command_name == "test_stub"
        assert results[0].valid_values == ["a", "b"]

    def test_optional_missing_param_skipped(self):
        cmd = StubCommand([
            JarvisParameter("opt", "string", required=False, enum_values=["x"]),
        ])
        results = cmd.validate_call()
        assert results == []

    def test_multiple_errors_collected(self):
        cmd = StubCommand([
            JarvisParameter("a", "string", required=True, enum_values=["x"]),
            JarvisParameter("b", "string", required=True, enum_values=["y"]),
        ])
        results = cmd.validate_call(a="bad", b="wrong")
        assert len(results) == 2
        assert all(not r.success for r in results)


# ---------------------------------------------------------------------------
# execute() integration — validation wired into flow
# ---------------------------------------------------------------------------

class TestExecuteValidation:

    def test_execute_with_enum_error_returns_validation_error(self):
        cmd = StubCommand([
            JarvisParameter("mode", "string", required=True, enum_values=["fast", "slow"]),
        ])
        request_info = RequestInformation(
            voice_command="test", conversation_id="conv-1"
        )
        response = cmd.execute(request_info, mode="invalid")

        assert response.success is False
        assert response.context_data["_validation_error"] is True
        assert len(response.context_data["errors"]) == 1
        assert response.context_data["errors"][0]["param"] == "mode"

    def test_execute_auto_correction_applied(self):
        """Auto-corrections from validate_call are applied to kwargs."""
        cmd = StubCommand([
            JarvisParameter("name", "string", required=True),
        ])

        # Override validate_call to return a suggested_value
        original_validate = cmd.validate_call

        def mock_validate(**kwargs):
            return [ValidationResult(
                success=True,
                param_name="name",
                command_name="test_stub",
                suggested_value="corrected_name",
            )]

        cmd.validate_call = mock_validate

        request_info = RequestInformation(
            voice_command="test", conversation_id="conv-1"
        )
        response = cmd.execute(request_info, name="wrong_name")

        assert response.success is True
        # The corrected value should be in context_data (passed through to run)
        assert response.context_data["name"] == "corrected_name"

    def test_execute_valid_params_reaches_run(self):
        cmd = StubCommand([
            JarvisParameter("x", "string", required=True, enum_values=["ok"]),
        ])
        request_info = RequestInformation(
            voice_command="test", conversation_id="conv-1"
        )
        response = cmd.execute(request_info, x="ok")
        assert response.success is True


# ---------------------------------------------------------------------------
# CommandResponse.validation_error()
# ---------------------------------------------------------------------------

class TestCommandResponseValidationError:

    def test_creates_error_response(self):
        results = [
            ValidationResult(
                success=False,
                param_name="entity_id",
                command_name="control_device",
                message="Entity not found",
                valid_values=["light.a"],
            ),
        ]
        response = CommandResponse.validation_error(results)

        assert response.success is False
        assert response.wait_for_input is False
        assert response.context_data["_validation_error"] is True
        assert len(response.context_data["errors"]) == 1
        assert response.context_data["errors"][0]["param"] == "entity_id"
        assert response.context_data["errors"][0]["message"] == "Entity not found"
        assert "Entity not found" in response.error_details

    def test_multiple_errors(self):
        results = [
            ValidationResult(success=False, param_name="a", command_name="cmd", message="err1"),
            ValidationResult(success=False, param_name="b", command_name="cmd", message="err2"),
        ]
        response = CommandResponse.validation_error(results)

        assert len(response.context_data["errors"]) == 2
        assert "err1" in response.error_details
        assert "err2" in response.error_details

    def test_filters_success_results(self):
        results = [
            ValidationResult(success=True, param_name="x", command_name="cmd"),
            ValidationResult(success=False, param_name="y", command_name="cmd", message="bad"),
        ]
        response = CommandResponse.validation_error(results)

        assert len(response.context_data["errors"]) == 1
        assert response.context_data["errors"][0]["param"] == "y"


# ---------------------------------------------------------------------------
# ControlDeviceCommand.validate_call() — HA entity validation
# ---------------------------------------------------------------------------

class TestControlDeviceValidateCall:

    @pytest.fixture
    def command(self):
        from commands.control_device_command import ControlDeviceCommand
        return ControlDeviceCommand()

    def test_empty_entity_id_fails(self, command):
        results = command.validate_call(entity_id="")
        errors = [r for r in results if not r.success]
        assert len(errors) == 1
        assert errors[0].param_name == "entity_id"
        assert "required" in errors[0].message.lower()

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_known_entity_passes(self, mock_known, command):
        mock_known.return_value = {"light.office": "Office"}
        results = command.validate_call(entity_id="light.office")
        errors = [r for r in results if not r.success]
        assert errors == []

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_unknown_entity_no_match_returns_error(self, mock_known, command):
        mock_known.return_value = {
            "light.kitchen": "Kitchen",
            "light.bedroom": "Bedroom",
        }
        results = command.validate_call(entity_id="light.nonexistent_xyz")
        errors = [r for r in results if not r.success]
        assert len(errors) == 1
        assert "not found" in errors[0].message.lower()
        assert errors[0].valid_values is not None

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_auto_correct_unambiguous(self, mock_known, command):
        mock_known.return_value = {
            "light.my_office": "My Office",
            "light.kitchen": "Kitchen",
        }
        results = command.validate_call(entity_id="light.office")
        # Should be auto-corrected (containment match: "office" in "my_office")
        corrections = [r for r in results if r.suggested_value]
        assert len(corrections) == 1
        assert corrections[0].suggested_value == "light.my_office"
        assert corrections[0].success is True

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_no_known_entities_skips_validation(self, mock_known, command):
        mock_known.return_value = {}
        results = command.validate_call(entity_id="light.anything")
        errors = [r for r in results if not r.success]
        assert errors == []

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_ambiguous_match_returns_error(self, mock_known, command):
        mock_known.return_value = {
            "light.office_desk": "Office Desk",
            "light.office_fan": "Office Fan",
        }
        # "office" is contained in both, so ambiguous
        results = command.validate_call(entity_id="light.office")
        errors = [r for r in results if not r.success]
        assert len(errors) == 1
        assert "not found" in errors[0].message.lower()


# ---------------------------------------------------------------------------
# ControlDeviceCommand._find_best_match()
# ---------------------------------------------------------------------------

class TestFindBestMatch:

    def test_containment_match(self):
        from commands.control_device_command import ControlDeviceCommand
        known = {"light.my_office": "Office", "light.kitchen": "Kitchen"}
        result = ControlDeviceCommand._find_best_match("light.office", known)
        assert result == "light.my_office"

    def test_no_match(self):
        from commands.control_device_command import ControlDeviceCommand
        known = {"light.kitchen": "Kitchen", "light.bedroom": "Bedroom"}
        result = ControlDeviceCommand._find_best_match("light.garage", known)
        assert result is None

    def test_word_overlap_match(self):
        from commands.control_device_command import ControlDeviceCommand
        known = {"cover.garage_door": "Garage Door", "cover.blinds": "Blinds"}
        result = ControlDeviceCommand._find_best_match("cover.garage", known)
        assert result == "cover.garage_door"

    def test_no_dot_returns_none(self):
        from commands.control_device_command import ControlDeviceCommand
        known = {"light.office": "Office"}
        result = ControlDeviceCommand._find_best_match("nodot", known)
        assert result is None

    def test_ambiguous_returns_none(self):
        from commands.control_device_command import ControlDeviceCommand
        # Both have equal word overlap (1 word: "office")
        known = {
            "light.office_desk": "Office Desk",
            "light.office_fan": "Office Fan",
        }
        result = ControlDeviceCommand._find_best_match("light.office", known)
        # "office" is contained in both slugs → both score 2 → ambiguous
        assert result is None

    def test_cross_domain_ignored(self):
        from commands.control_device_command import ControlDeviceCommand
        known = {"switch.office": "Office Switch", "light.office": "Office Light"}
        result = ControlDeviceCommand._find_best_match("light.my_office", known)
        # Only light.* candidates, switch.* excluded
        assert result == "light.office"


# ---------------------------------------------------------------------------
# GetDeviceStatusCommand.validate_call()
# ---------------------------------------------------------------------------

class TestGetDeviceStatusValidateCall:

    @pytest.fixture
    def command(self):
        from commands.get_device_status_command import GetDeviceStatusCommand
        return GetDeviceStatusCommand()

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_known_entity_passes(self, mock_known, command):
        mock_known.return_value = {"light.office": "Office"}
        results = command.validate_call(entity_id="light.office")
        errors = [r for r in results if not r.success]
        assert errors == []

    @patch("commands.control_device_command.ControlDeviceCommand._get_known_entities")
    def test_unknown_entity_returns_error(self, mock_known, command):
        mock_known.return_value = {"light.kitchen": "Kitchen"}
        results = command.validate_call(entity_id="light.xyz")
        errors = [r for r in results if not r.success]
        assert len(errors) == 1
        assert "not found" in errors[0].message.lower()
