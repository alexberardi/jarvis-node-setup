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
        return "Two-number arithmetic: add, subtract, multiply, or divide. Not for unit conversions or advanced formulas."
    
    @property
    def allow_direct_answer(self) -> bool:
        return True

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
            JarvisParameter("num1", "float", required=True, description="First number."),
            JarvisParameter("num2", "float", required=True, description="Second number."),
            JarvisParameter("operation", "string", required=True, description="Operation: strictly 'add', 'subtract', 'multiply', or 'divide'.")
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
        """Generate varied examples for adapter training"""
        items = [
            ("What's 5 plus 3?", 5, 3, "add"),
            ("Add 7 and 9", 7, 9, "add"),
            ("Calculate 18 plus 4", 18, 4, "add"),
            ("What is 12 + 6?", 12, 6, "add"),
            ("Sum 21 and 14", 21, 14, "add"),
            ("What's 10 minus 4?", 10, 4, "subtract"),
            ("Subtract 7 from 22", 22, 7, "subtract"),
            ("Calculate 50 minus 13", 50, 13, "subtract"),
            ("What is 99 - 45?", 99, 45, "subtract"),
            ("What's the difference between 60 and 18?", 60, 18, "subtract"),
            ("What is 6 times 7?", 6, 7, "multiply"),
            ("Multiply 9 and 8", 9, 8, "multiply"),
            ("Calculate 12 * 4", 12, 4, "multiply"),
            ("What's 14 times 3?", 14, 3, "multiply"),
            ("Product of 11 and 5", 11, 5, "multiply"),
            ("Divide 20 by 5", 20, 5, "divide"),
            ("What is 81 divided by 9?", 81, 9, "divide"),
            ("Calculate 100 / 4", 100, 4, "divide"),
            ("Divide 72 by 8", 72, 8, "divide"),
            ("Quotient of 49 and 7", 49, 7, "divide"),
            ("Add 3.5 and 2.1", 3.5, 2.1, "add"),
            ("What's 7.2 minus 1.1?", 7.2, 1.1, "subtract"),
            ("Multiply 2.5 by 4", 2.5, 4, "multiply"),
            ("Divide 9.6 by 3.2", 9.6, 3.2, "divide"),
            ("What's the sum of 8 and 12?", 8, 12, "add"),
            ("Compute 23 plus 19", 23, 19, "add"),
            ("Calculate ten minus four", 10, 4, "subtract"),
            ("What's five plus three?", 5, 3, "add"),
            ("What is 6 times 7 again?", 6, 7, "multiply"),
            ("Divide 144 by 12", 144, 12, "divide"),
            ("Add 15 and 25 together", 15, 25, "add"),
            ("Subtract 30 from 100", 100, 30, "subtract"),
            ("Multiply 16 by 2", 16, 2, "multiply"),
            ("Divide 45 by 5", 45, 5, "divide"),
            ("What is 3 plus 0?", 3, 0, "add"),
            ("What is 9 minus 9?", 9, 9, "subtract"),
            ("What is 1 times 12?", 1, 12, "multiply"),
            ("What is 42 divided by 6?", 42, 6, "divide"),
            ("What's 15 percent of 200?", 0.15, 200, "multiply"),
            # Casual/varied phrasings (written-out numbers, colloquial)
            ("What's five plus three?", 5, 3, "add"),
            ("What's negative ten times four?", -10, 4, "multiply"),
            ("How much is a dozen times twelve?", 12, 12, "multiply"),
            ("What's half of a hundred?", 100, 2, "divide"),
            ("Double seventy-three", 73, 2, "multiply"),
            ("Triple fifteen", 15, 3, "multiply"),
            ("What do I get if I add seven and eight?", 7, 8, "add"),
            ("Take away twelve from fifty", 50, 12, "subtract"),
            ("How many is six times nine?", 6, 9, "multiply"),
            ("Minus twenty plus thirty", -20, 30, "add"),
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
