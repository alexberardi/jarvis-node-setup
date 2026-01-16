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
from clients.responses.jarvis_command_center import DateContext


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
        return "Two-number arithmetic: add, subtract, multiply, or divide. Use for simple math. Do NOT use for unit conversions or multi-step/advanced formulas."
    
    @property
    def keywords(self) -> List[str]:
        return [
            "calculate", "math", "add", "subtract", "multiply", "divide",
            "plus", "minus", "times", "divided by", "sum", "difference",
            "product", "quotient", "arithmetic", "computation",
            "percent", "percentage", "%", "percent of", "of"
        ]
    
    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter("num1", "float", required=True, description="The first number in the calculation. Can be positive, negative, integer, or decimal (e.g., 5, -3.14, 42.5)"),
            JarvisParameter("num2", "float", required=True, description="The second number in the calculation. Can be positive, negative, integer, or decimal (e.g., 3, -1.5, 100)"),
            JarvisParameter("operation", "string", required=True, description="The arithmetic operation to perform. Must be exactly one of: 'add' (addition/sum), 'subtract' (subtraction/difference), 'multiply' (multiplication/product), 'divide' (division/quotient)")
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
    
    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate examples for the calculator command"""
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
                voice_command="Add 15 and 25 together",
                expected_parameters={"num1": 15, "num2": 25, "operation": "add"}
            ),
            CommandExample(
                voice_command="What's five plus 3?",
                expected_parameters={"num1": 5, "num2": 3, "operation": "add"}
            ),
            CommandExample(
                voice_command="Calculate ten minus four",
                expected_parameters={"num1": 10, "num2": 4, "operation": "subtract"}
            ),
            CommandExample(
                voice_command="What's 15 percent of 200?",
                expected_parameters={"num1": 0.15, "num2": 200, "operation": "multiply"}
            ),
            CommandExample(
                voice_command="Calculate 25% of 80",
                expected_parameters={"num1": 0.25, "num2": 80, "operation": "multiply"}
            ),
            CommandExample(
                voice_command="What is 10% of 150?",
                expected_parameters={"num1": 0.1, "num2": 150, "operation": "multiply"}
            )
        ]
    
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
