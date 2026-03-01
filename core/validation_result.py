"""Validation result for parameter validation in IJarvisCommand."""

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """Result of validating a single parameter value.

    Used by validate_call() to report validation outcomes.
    Multiple results are collected and either auto-corrected
    or sent back to the CC as validation errors for LLM retry.
    """

    success: bool
    param_name: str
    command_name: str
    message: str | None = None
    suggested_value: str | None = None
    valid_values: list[str] | None = field(default=None, repr=False)
