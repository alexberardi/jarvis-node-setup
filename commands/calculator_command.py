#!/usr/bin/env python3
"""
Calculator command for Jarvis.
Performs basic arithmetic operations on two numbers.
"""

from typing import List
from enum import Enum
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation


class Operation(Enum):
    """Supported arithmetic operations"""
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class CalculatorCommand(IJarvisCommand):
    """Command for performing basic arithmetic calculations"""
    
    @property
    def command_name(self) -> str:
        return "calculate"
    
    @property
    def description(self) -> str:
        return "Perform two-number arithmetic operations: addition, subtraction, multiplication, or division."
    
    @property
    def allow_direct_answer(self) -> bool:
        return True

    @property
    def keywords(self) -> List[str]:
        return [
            "calculate", "math", "add", "subtract", "multiply", "divide",
            "plus", "minus"
        ]
    
    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter("num1", "float", required=True, description="First number."),
            JarvisParameter("num2", "float", required=True, description="Second number."),
            JarvisParameter("operation", "string", required=True, description="Operation: must be exactly 'add', 'subtract', 'multiply', or 'divide' (no synonyms).")
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []
    
    @property
    def critical_rules(self) -> List[str]:
        return [
            "Map common operation terms to exact values: 'sum'/'plus'/'+' → 'add', 'minus'/'-' → 'subtract', 'times'/'*' → 'multiply', 'divided by'/'/' → 'divide'",
            "The operation parameter must be exactly one of: 'add', 'subtract', 'multiply', 'divide'"
        ]
    
    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for the calculator command"""
        return [
            CommandExample(
                voice_command="What's 5 plus 3?",
                expected_parameters={"num1": 5, "num2": 3, "operation": "add"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Calculate 10 minus 4",
                expected_parameters={"num1": 10, "num2": 4, "operation": "subtract"}
            ),
            CommandExample(
                voice_command="What is 6 times 7?",
                expected_parameters={"num1": 6, "num2": 7, "operation": "multiply"}
            ),
            CommandExample(
                voice_command="Divide 20 by 5",
                expected_parameters={"num1": 20, "num2": 5, "operation": "divide"}
            ),
            CommandExample(
                voice_command="What's 15 percent of 200?",
                expected_parameters={"num1": 0.15, "num2": 200, "operation": "multiply"}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Consolidated for 3B model (13 examples):
        - One per operation pattern (add/subtract/multiply/divide)
        - Floating point, written-out numbers, percentages, casual
        """
        items = [
            # One per operation
            ("What's 7 plus 9?", 7, 9, "add"),
            ("Add 18 and 4", 18, 4, "add"),
            ("What's 50 minus 13?", 50, 13, "subtract"),
            ("Subtract 7 from 22", 22, 7, "subtract"),
            ("What's 9 times 8?", 9, 8, "multiply"),
            ("What is 81 divided by 9?", 81, 9, "divide"),
            ("Divide 72 by 8", 72, 8, "divide"),

            # Floating point
            ("Add 3.5 and 2.1", 3.5, 2.1, "add"),

            # Written-out numbers
            ("What's seven times nine?", 7, 9, "multiply"),

            # Percentage (maps to multiply)
            ("What's 20 percent of 150?", 0.20, 150, "multiply"),

            # Casual / shorthand
            ("Double forty-two", 42, 2, "multiply"),
            ("Half of sixty", 60, 2, "divide"),

            # Large numbers
            ("What's 1000 plus 2500?", 1000, 2500, "add"),
        ]
        examples = []
        for i, (utterance, num1, num2, op) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters={"num1": num1, "num2": num2, "operation": op},
                is_primary=(i == 0)
            ))
        return examples
    
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the calculator command"""
        try:
            # Extract parameters from kwargs
            num1 = float(kwargs.get("num1"))
            num2 = float(kwargs.get("num2"))
            operation_str = kwargs.get("operation", "").lower()
            
            # Validate operation
            try:
                operation = Operation(operation_str)
            except ValueError:
                return CommandResponse.error_response(
                                        error_details=f"Invalid operation: {operation_str}",
                    context_data={
                        "num1": num1,
                        "num2": num2,
                        "operation": operation_str,
                        "error": "Invalid operation"
                    }
                )
            
            # Perform calculation
            if operation == Operation.ADD:
                result = num1 + num2
                operation_text = "plus"
            elif operation == Operation.SUBTRACT:
                result = num1 - num2
                operation_text = "minus"
            elif operation == Operation.MULTIPLY:
                result = num1 * num2
                operation_text = "times"
            elif operation == Operation.DIVIDE:
                if num2 == 0:
                    return CommandResponse.error_response(
                                                error_details="Division by zero",
                        context_data={
                            "num1": num1,
                            "num2": num2,
                            "operation": operation_str,
                            "error": "Division by zero"
                        }
                    )
                result = num1 / num2
                operation_text = "divided by"
            else:
                return CommandResponse.error_response(
                                        error_details=f"Unsupported operation: {operation}",
                    context_data={
                        "num1": num1,
                        "num2": num2,
                        "operation": operation_str,
                        "error": "Unsupported operation"
                    }
                )
            
            # Format response
            if operation == Operation.DIVIDE and result != float(result):
                # Show decimal for division results that aren't whole numbers
                result_text = f"{result:.2f}"
            else:
                result_text = str(float(result))
            
            # Create the calculation message
            calculation_message = f"{num1} {operation_text} {num2} equals {result_text}"
            
            return CommandResponse.follow_up_response(
                                context_data={
                    "result": result,
                    "calculation": f"{num1} {operation_text} {num2} = {result_text}",
                    "operation": operation.value,
                    "num1": num1,
                    "num2": num2,
                    "operation_text": operation_text
                }
            )
            
        except (ValueError, TypeError) as e:
            return CommandResponse.error_response(
                                error_details=f"Invalid parameters: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )
        except Exception as e:
            return CommandResponse.error_response(
                                error_details=f"Calculation error: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )
