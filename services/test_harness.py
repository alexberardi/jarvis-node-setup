#!/usr/bin/env python3
"""Test harness that runs inside the Docker container during command testing.

This script validates that a command is well-formed and safe:
- Imports successfully
- Class instantiates
- Properties return correct types
- Schema generation works
- Mock execution doesn't crash

Exit code 0 = all tests pass, non-zero = failures.
"""

import json
import sys
import time
import traceback
from pathlib import Path

# The command module is mounted at /test/command/
sys.path.insert(0, "/test/command")
sys.path.insert(0, "/test")


def _find_command_class():
    """Find the IJarvisCommand subclass in the command module."""
    from jarvis_command_sdk import IJarvisCommand

    import importlib
    mod = importlib.import_module("command")

    for attr_name in dir(mod):
        cls = getattr(mod, attr_name)
        if (isinstance(cls, type)
                and issubclass(cls, IJarvisCommand)
                and cls is not IJarvisCommand):
            return cls
    return None


def run_tests() -> dict:
    """Run all behavioral tests and return results."""
    results = {
        "tests": [],
        "passed": 0,
        "failed": 0,
        "errors": [],
    }

    def record(name: str, passed: bool, error: str | None = None) -> None:
        results["tests"].append({"name": name, "passed": passed, "error": error})
        if passed:
            results["passed"] += 1
        else:
            results["failed"] += 1
            if error:
                results["errors"].append(f"{name}: {error}")

    # Test 1: Import and find class
    try:
        cls = _find_command_class()
        if cls is None:
            record("import_and_find_class", False, "No IJarvisCommand subclass found in command.py")
            results["summary"] = "FAIL - No command class found"
            return results
        record("import_and_find_class", True)
    except Exception as e:
        record("import_and_find_class", False, f"Import failed: {e}")
        results["summary"] = f"FAIL - Import error: {e}"
        return results

    # Test 2: Instantiate
    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        results["summary"] = f"FAIL - Cannot instantiate: {e}"
        return results

    # Test 3: command_name is a non-empty string
    try:
        name = instance.command_name
        assert isinstance(name, str) and len(name) > 0, f"command_name must be non-empty str, got {type(name)}: {name!r}"
        record("command_name", True)
    except Exception as e:
        record("command_name", False, str(e))

    # Test 4: description is a non-empty string
    try:
        desc = instance.description
        assert isinstance(desc, str) and len(desc) > 0, f"description must be non-empty str, got {type(desc)}: {desc!r}"
        record("description", True)
    except Exception as e:
        record("description", False, str(e))

    # Test 5: parameters is a list
    try:
        params = instance.parameters
        assert isinstance(params, list), f"parameters must be list, got {type(params)}"
        record("parameters_type", True)
    except Exception as e:
        record("parameters_type", False, str(e))

    # Test 6: required_secrets is a list
    try:
        secrets = instance.required_secrets
        assert isinstance(secrets, list), f"required_secrets must be list, got {type(secrets)}"
        record("required_secrets_type", True)
    except Exception as e:
        record("required_secrets_type", False, str(e))

    # Test 7: keywords is a list of strings
    try:
        kw = instance.keywords
        assert isinstance(kw, list), f"keywords must be list, got {type(kw)}"
        assert all(isinstance(k, str) for k in kw), "All keywords must be strings"
        record("keywords_type", True)
    except Exception as e:
        record("keywords_type", False, str(e))

    # Test 8: generate_prompt_examples returns list of CommandExample
    try:
        examples = instance.generate_prompt_examples()
        assert isinstance(examples, list), f"generate_prompt_examples must return list, got {type(examples)}"
        assert len(examples) > 0, "Must have at least one prompt example"
        for ex in examples:
            assert hasattr(ex, "voice_command"), "Examples must have voice_command"
            assert hasattr(ex, "expected_parameters"), "Examples must have expected_parameters"
        record("prompt_examples", True)
    except Exception as e:
        record("prompt_examples", False, str(e))

    # Test 9: generate_adapter_examples returns list
    try:
        adapter_ex = instance.generate_adapter_examples()
        assert isinstance(adapter_ex, list), f"generate_adapter_examples must return list, got {type(adapter_ex)}"
        record("adapter_examples", True)
    except Exception as e:
        record("adapter_examples", False, str(e))

    # Test 10: validate_call doesn't crash
    try:
        result = instance.validate_call()
        assert isinstance(result, list), f"validate_call must return list, got {type(result)}"
        record("validate_call", True)
    except Exception as e:
        record("validate_call", False, str(e))

    # Test 11: Timed mock run (with sample parameters)
    try:
        from jarvis_command_sdk import RequestInformation
        ri = RequestInformation(voice_command="test", conversation_id="test-conv")

        # Build sample kwargs from first example if available
        kwargs = {}
        if examples and len(examples) > 0:
            kwargs = dict(examples[0].expected_parameters)

        start = time.time()
        # We don't expect run() to succeed (secrets not configured etc)
        # but it shouldn't hang or crash catastrophically
        try:
            resp = instance.run(ri, **kwargs)
        except Exception:
            pass  # Expected — secrets missing, etc.
        elapsed = time.time() - start

        if elapsed > 30:
            record("mock_run_timing", False, f"run() took {elapsed:.1f}s (>30s limit)")
        else:
            record("mock_run_timing", True)
    except Exception as e:
        record("mock_run_timing", False, str(e))

    # Summary
    results["summary"] = f"{'PASS' if results['failed'] == 0 else 'FAIL'} - {results['passed']}/{results['passed'] + results['failed']} tests passed"
    return results


if __name__ == "__main__":
    try:
        results = run_tests()
        print(json.dumps(results, indent=2))
        sys.exit(0 if results["failed"] == 0 else 1)
    except Exception as e:
        print(json.dumps({
            "summary": f"CRASH - {e}",
            "tests": [],
            "passed": 0,
            "failed": 1,
            "errors": [traceback.format_exc()],
        }, indent=2))
        sys.exit(2)
