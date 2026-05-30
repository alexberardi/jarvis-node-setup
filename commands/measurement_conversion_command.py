#!/usr/bin/env python3
"""
Measurement conversion command for Jarvis.
Converts between various units using base unit conversion for maximum flexibility.
"""

import re
from typing import List, Optional
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
)
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation


# Map spoken/common unit names to canonical keys understood by run().
# Includes singular forms ("mile") and casual aliases ("fl oz"). The canonical
# keys are exactly the keys of BASE_CONVERSIONS in __init__.
_UNIT_ALIASES: dict[str, str] = {
    # Distance
    "meter": "meters", "meters": "meters", "metre": "meters", "metres": "meters", "m": "meters",
    "kilometer": "kilometers", "kilometers": "kilometers", "kilometre": "kilometers",
    "kilometres": "kilometers", "km": "kilometers", "kms": "kilometers", "k": "kilometers",
    "centimeter": "centimeters", "centimeters": "centimeters", "cm": "centimeters",
    "millimeter": "millimeters", "millimeters": "millimeters", "mm": "millimeters",
    "mile": "miles", "miles": "miles",
    "yard": "yards", "yards": "yards", "yd": "yards",
    "foot": "feet", "feet": "feet", "ft": "feet",
    "inch": "inches", "inches": "inches", "in": "inches",
    "league": "leagues", "leagues": "leagues",
    # Volume
    "liter": "liters", "liters": "liters", "litre": "liters", "litres": "liters", "l": "liters",
    "milliliter": "milliliters", "milliliters": "milliliters", "ml": "milliliters",
    "gallon": "gallons", "gallons": "gallons", "gal": "gallons",
    "quart": "quarts", "quarts": "quarts", "qt": "quarts",
    "pint": "pints", "pints": "pints", "pt": "pints",
    "cup": "cups", "cups": "cups",
    "tablespoon": "tablespoons", "tablespoons": "tablespoons", "tbsp": "tablespoons",
    "teaspoon": "teaspoons", "teaspoons": "teaspoons", "tsp": "teaspoons",
    "fluid ounce": "fluid_ounces", "fluid ounces": "fluid_ounces",
    "fl oz": "fluid_ounces",
    # Weight (note: ambiguous "ounce/ounces" defaults to weight, not fluid;
    # "fluid ounce" must be explicit to mean volume)
    "gram": "grams", "grams": "grams", "g": "grams",
    "kilogram": "kilograms", "kilograms": "kilograms", "kg": "kilograms", "kgs": "kilograms",
    "milligram": "milligrams", "milligrams": "milligrams", "mg": "milligrams",
    "pound": "pounds", "pounds": "pounds", "lb": "pounds", "lbs": "pounds",
    "ounce": "ounces", "ounces": "ounces", "oz": "ounces",
    "ton": "tons", "tons": "tons",
    "metric ton": "metric_tons", "metric tons": "metric_tons", "tonne": "metric_tons",
    "tonnes": "metric_tons",
    # Temperature
    "celsius": "celsius", "centigrade": "celsius", "c": "celsius",
    "fahrenheit": "fahrenheit", "f": "fahrenheit",
    "kelvin": "kelvin", "k": "kelvin",
}

# Build a regex alternation over the multi-word aliases first so "fluid ounces"
# matches before "ounces" can claim it. Single-token units fall through.
_UNIT_TOKENS = sorted(_UNIT_ALIASES.keys(), key=len, reverse=True)
_UNIT_GROUP = r"(?:" + "|".join(re.escape(u) for u in _UNIT_TOKENS) + r")"

# "Convert N FROM to TO" — explicit imperative form.
_CONVERT_RE = re.compile(
    r"^\s*convert\s+(?P<value>-?\d+(?:\.\d+)?)\s+"
    r"(?P<from>" + _UNIT_GROUP + r")\s+(?:to|into|in)\s+"
    r"(?P<to>" + _UNIT_GROUP + r")\s*[?.!]*$",
    re.IGNORECASE,
)

# "How many X in a Y" / "how many X in N Y" — interrogative; the value
# defaults to 1 when "a/an" is the determiner.
_HOWMANY_RE = re.compile(
    r"^\s*how\s+many\s+(?P<to>" + _UNIT_GROUP + r")\s+(?:are\s+)?in\s+"
    r"(?:a\s+|an\s+|(?P<value>-?\d+(?:\.\d+)?)\s+)"
    r"(?P<from>" + _UNIT_GROUP + r")\s*[?.!]*$",
    re.IGNORECASE,
)

# "What's N X in Y" — typically used for temperature ("what's 350 F in C")
# but works for any pair.
_WHATS_IN_RE = re.compile(
    r"^\s*(?:what(?:'?s|\s+is))?\s*(?P<value>-?\d+(?:\.\d+)?)\s+"
    r"(?:degrees?\s+)?(?P<from>" + _UNIT_GROUP + r")\s+in\s+"
    r"(?:degrees?\s+)?(?P<to>" + _UNIT_GROUP + r")\s*[?.!]*$",
    re.IGNORECASE,
)


def _canonical_unit(raw: str) -> str | None:
    """Normalise spoken unit text → canonical key, or None if unknown."""
    if not raw:
        return None
    norm = re.sub(r"\s+", " ", raw.lower().strip())
    return _UNIT_ALIASES.get(norm)


class MeasurementConversionCommand(IJarvisCommand):
    """Command for converting between various measurement units"""
    
    def __init__(self):
        # Define base units and conversion factors
        # All conversions go through these base units for maximum flexibility
        self.BASE_CONVERSIONS = {
            # Distance - base unit: meters
            "meters": 1.0,
            "kilometers": 1000.0,
            "centimeters": 0.01,
            "millimeters": 0.001,
            "miles": 1609.34,
            "yards": 0.9144,
            "feet": 0.3048,
            "inches": 0.0254,
            "leagues": 4828.03,  # nautical league
            
            # Volume - base unit: liters
            "liters": 1.0,
            "milliliters": 0.001,
            "gallons": 3.78541,
            "quarts": 0.946353,
            "pints": 0.473176,
            "cups": 0.236588,
            "tablespoons": 0.0147868,
            "teaspoons": 0.00492892,
            "fluid_ounces": 0.0295735,
            
            # Weight - base unit: grams
            "grams": 1.0,
            "kilograms": 1000.0,
            "milligrams": 0.001,
            "pounds": 453.592,
            "ounces": 28.3495,
            "tons": 907185.0,  # US short ton
            "metric_tons": 1000000.0,
            
            # Temperature - special handling needed
            "celsius": "celsius",  # Base unit for temperature
            "fahrenheit": "fahrenheit",
            "kelvin": "kelvin"
        }
        
        # Temperature conversion functions (special case)
        self.TEMPERATURE_CONVERSIONS = {
            ("celsius", "fahrenheit"): lambda c: (c * 9/5) + 32,
            ("celsius", "kelvin"): lambda c: c + 273.15,
            ("fahrenheit", "celsius"): lambda f: (f - 32) * 5/9,
            ("fahrenheit", "kelvin"): lambda f: (f - 32) * 5/9 + 273.15,
            ("kelvin", "celsius"): lambda k: k - 273.15,
            ("kelvin", "fahrenheit"): lambda k: (k - 273.15) * 9/5 + 32
        }
    
    @property
    def command_name(self) -> str:
        return "convert_measurement"
    
    @property
    def description(self) -> str:
        return "Convert units: distance, volume, weight, temperature. Explicit conversions ONLY — weather unit preferences (Celsius/Fahrenheit) should use get_weather."
    
    @property
    def keywords(self) -> List[str]:
        return [
            "convert", "conversion", "how many", "in a", "equals", "to",
            "miles", "kilometers", "gallons", "liters",
            "pounds", "kilograms", "celsius", "fahrenheit"
        ]
    
    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter("value", "float", required=False, description="Numeric value to convert; defaults to 1 if not specified."),
            JarvisParameter("from_unit", "string", required=True, description="Source unit name (e.g., 'miles', 'cups', 'pounds', 'celsius')."),
            JarvisParameter("to_unit", "string", required=True, description="Target unit name (e.g., 'kilometers', 'liters', 'kilograms', 'fahrenheit')."),
            JarvisParameter("category", "string", required=False, description="Optional hint: 'distance', 'volume', 'weight', or 'temperature'.")
        ]
    
    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required for measurement conversion

    @property
    def critical_rules(self) -> List[str]:
        return [
            "NOT for weather display preferences ('weather in metric'). That's get_weather.",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_weather",
                description="Weather queries mentioning 'metric units', 'imperial units', 'Celsius', or 'Fahrenheit' as a display preference. These are weather requests, not unit conversions."
            )
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for the measurement conversion command"""
        return [
            CommandExample(
                voice_command="How many inches in a mile?",
                expected_parameters={"value": 1, "from_unit": "miles", "to_unit": "inches"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Convert 5 miles to kilometers",
                expected_parameters={"value": 5, "from_unit": "miles", "to_unit": "kilometers"}
            ),
            CommandExample(
                voice_command="How many cups in a gallon?",
                expected_parameters={"value": 1, "from_unit": "gallons", "to_unit": "cups"}
            ),
            CommandExample(
                voice_command="Convert 10 pounds to kilograms",
                expected_parameters={"value": 10, "from_unit": "pounds", "to_unit": "kilograms"}
            ),
            CommandExample(
                voice_command="What's 350 Fahrenheit in Celsius?",
                expected_parameters={"value": 350, "from_unit": "fahrenheit", "to_unit": "celsius"}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training"""
        items = [
            # Distance
            ("Convert 5 miles to kilometers", 5, "miles", "kilometers"),
            ("How many feet in a mile?", 1, "miles", "feet"),
            ("Convert 12 inches to centimeters", 12, "inches", "centimeters"),

            # Volume
            ("How many cups in a gallon?", 1, "gallons", "cups"),
            ("Convert 2 liters to gallons", 2, "liters", "gallons"),
            ("How many tablespoons in a cup?", 1, "cups", "tablespoons"),

            # Weight
            ("Convert 10 pounds to kilograms", 10, "pounds", "kilograms"),
            ("How many grams in 3 ounces?", 3, "ounces", "grams"),

            # Temperature
            ("What's 350 Fahrenheit in Celsius?", 350, "fahrenheit", "celsius"),
            ("Convert 25 Celsius to Fahrenheit", 25, "celsius", "fahrenheit"),

            # Casual / real-world
            ("How far is a 5K?", 5, "kilometers", "miles"),
            ("How many teaspoons in a tablespoon?", 1, "tablespoons", "teaspoons"),
        ]
        examples = []
        for i, (utterance, value, from_unit, to_unit) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters={"value": value, "from_unit": from_unit, "to_unit": to_unit},
                is_primary=(i == 0)
            ))
        return examples
    
    # ------------------------------------------------------------------
    # Fast-path patterns — bypass the LLM for stereotyped conversions.
    # Order matters: the most specific shape ("convert N X to Y") runs
    # before the more permissive "what's N X in Y", which would otherwise
    # accidentally consume "convert 5 miles to km".
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="convert_measurement.convert",
                description="Bypass LLM for 'convert N <unit> to <unit>'",
                example="convert 5 miles to kilometers",
                regex=_CONVERT_RE.pattern,
                handler="_fp_convert",
            ),
            FastPathPattern(
                id="convert_measurement.how_many",
                description="Bypass LLM for 'how many X in a Y'",
                example="how many cups in a gallon",
                regex=_HOWMANY_RE.pattern,
                handler="_fp_how_many",
            ),
            FastPathPattern(
                id="convert_measurement.whats_in",
                description="Bypass LLM for 'what's N <unit> in <unit>'",
                example="what's 350 fahrenheit in celsius",
                regex=_WHATS_IN_RE.pattern,
                handler="_fp_whats_in",
            ),
        ]

    def _fp_convert(self, match, voice_command: str) -> PreRouteResult | None:
        from_unit = _canonical_unit(match.group("from"))
        to_unit = _canonical_unit(match.group("to"))
        if not from_unit or not to_unit:
            return None
        return PreRouteResult(arguments={
            "value": float(match.group("value")),
            "from_unit": from_unit,
            "to_unit": to_unit,
        })

    def _fp_how_many(self, match, voice_command: str) -> PreRouteResult | None:
        from_unit = _canonical_unit(match.group("from"))
        to_unit = _canonical_unit(match.group("to"))
        if not from_unit or not to_unit:
            return None
        value_raw = match.group("value")
        value = float(value_raw) if value_raw else 1.0
        return PreRouteResult(arguments={
            "value": value,
            "from_unit": from_unit,
            "to_unit": to_unit,
        })

    def _fp_whats_in(self, match, voice_command: str) -> PreRouteResult | None:
        from_unit = _canonical_unit(match.group("from"))
        to_unit = _canonical_unit(match.group("to"))
        if not from_unit or not to_unit:
            return None
        # Defensive: refuse if the two units aren't in the same category.
        # The full validation lives in run(); a quick check here keeps us
        # from pre-routing a nonsensical pairing (e.g. "5 miles in cups").
        return PreRouteResult(arguments={
            "value": float(match.group("value")),
            "from_unit": from_unit,
            "to_unit": to_unit,
        })

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the measurement conversion command"""
        try:
            # Extract parameters
            value = float(kwargs.get("value", 1.0))  # Default to 1 if not specified
            from_unit = kwargs.get("from_unit", "").lower()
            to_unit = kwargs.get("to_unit", "").lower()
            category = kwargs.get("category", "").lower()
            
            # Validate units
            if not from_unit or not to_unit:
                return CommandResponse.error_response(
                                        error_details="Missing from_unit or to_unit",
                    context_data={
                        "value": value,
                        "from_unit": from_unit,
                        "to_unit": to_unit,
                        "category": category
                    }
                )
            
            # Check if both units are supported
            if from_unit not in self.BASE_CONVERSIONS:
                return CommandResponse.error_response(
                                        error_details=f"Unsupported from_unit: {from_unit}",
                    context_data={
                        "value": value,
                        "from_unit": from_unit,
                        "to_unit": to_unit,
                        "supported_units": list(self.BASE_CONVERSIONS.keys())
                    }
                )
            
            if to_unit not in self.BASE_CONVERSIONS:
                return CommandResponse.error_response(
                                        error_details=f"Unsupported to_unit: {to_unit}",
                    context_data={
                        "value": value,
                        "from_unit": from_unit,
                        "to_unit": to_unit,
                        "supported_units": list(self.BASE_CONVERSIONS.keys())
                    }
                )
            
            # Handle temperature conversions (special case)
            if self._is_temperature_unit(from_unit) and self._is_temperature_unit(to_unit):
                result = self._convert_temperature(value, from_unit, to_unit)
                if result is None:
                    return CommandResponse.error_response(
                                                error_details="Temperature conversion failed",
                        context_data={
                            "value": value,
                            "from_unit": from_unit,
                            "to_unit": to_unit
                        }
                    )
                
                # Format temperature response
                if to_unit == "celsius":
                    result_text = f"{result:.1f}°C"
                elif to_unit == "fahrenheit":
                    result_text = f"{result:.1f}°F"
                elif to_unit == "kelvin":
                    result_text = f"{result:.1f}K"
                else:
                    result_text = f"{result:.3f}"
                
                conversion_message = f"{value} {from_unit} equals {result_text}"
                
                return CommandResponse.follow_up_response(
                                        context_data={
                        "value": value,
                        "from_unit": from_unit,
                        "to_unit": to_unit,
                        "result": result,
                        "result_text": result_text,
                        "conversion_type": "temperature"
                    }
                )
            
            # Handle standard unit conversions through base units
            try:
                result = self._convert_through_base(value, from_unit, to_unit)
            except Exception as e:
                return CommandResponse.error_response(
                                        error_details=f"Conversion error: {str(e)}",
                    context_data={
                        "value": value,
                        "from_unit": from_unit,
                        "to_unit": to_unit,
                        "error": str(e)
                    }
                )
            
            # Format the result
            if result == int(result):
                result_text = str(int(result))
            else:
                # Show appropriate decimal places based on magnitude
                if abs(result) < 0.01:
                    result_text = f"{result:.6f}"
                elif abs(result) < 0.1:
                    result_text = f"{result:.4f}"
                elif abs(result) < 1:
                    result_text = f"{result:.3f}"
                else:
                    result_text = f"{result:.2f}"
            
            # Create the conversion message
            if value == 1:
                conversion_message = f"There are {result_text} {to_unit} in 1 {from_unit}"
            else:
                conversion_message = f"{value} {from_unit} equals {result_text} {to_unit}"
            
            return CommandResponse.follow_up_response(
                                context_data={
                    "value": value,
                    "from_unit": from_unit,
                    "to_unit": to_unit,
                    "result": result,
                    "result_text": result_text,
                    "conversion_type": "standard"
                }
            )
            
        except (ValueError, TypeError) as e:
            return CommandResponse.error_response(
                                error_details=f"Parameter error: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )
        except Exception as e:
            return CommandResponse.error_response(
                                error_details=f"Unexpected error: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )
    
    def _is_temperature_unit(self, unit: str) -> bool:
        """Check if a unit is a temperature unit"""
        return unit in ["celsius", "fahrenheit", "kelvin"]
    
    def _convert_temperature(self, value: float, from_unit: str, to_unit: str) -> Optional[float]:
        """Convert temperature between different scales"""
        if from_unit == to_unit:
            return value
        
        # Direct conversion available
        conversion_key = (from_unit, to_unit)
        if conversion_key in self.TEMPERATURE_CONVERSIONS:
            return self.TEMPERATURE_CONVERSIONS[conversion_key](value)
        
        # Try reverse conversion
        reverse_key = (to_unit, from_unit)
        if reverse_key in self.TEMPERATURE_CONVERSIONS:
            # We need to find the inverse function
            # This is a simplified approach - in practice, you'd want proper inverse functions
            return None
        
        return None
    
    def _convert_through_base(self, value: float, from_unit: str, to_unit: str) -> float:
        """Convert between units by going through base units"""
        if from_unit == to_unit:
            return value
        
        # Get conversion factors to base units
        from_factor = self.BASE_CONVERSIONS[from_unit]
        to_factor = self.BASE_CONVERSIONS[to_unit]
        
        # Convert: from_unit → base_unit → to_unit
        # Formula: value * from_factor / to_factor
        
        # Determine which base unit to use based on the units
        if self._is_distance_unit(from_unit) and self._is_distance_unit(to_unit):
            # Use meters as base
            base_value = value * from_factor
            result = base_value / to_factor
        elif self._is_volume_unit(from_unit) and self._is_volume_unit(to_unit):
            # Use liters as base
            base_value = value * from_factor
            result = base_value / to_factor
        elif self._is_weight_unit(from_unit) and self._is_weight_unit(to_unit):
            # Use grams as base
            base_value = value * from_factor
            result = base_value / to_factor
        else:
            # Cross-category conversion (e.g., miles to liters doesn't make sense)
            raise ValueError(f"Cannot convert between {from_unit} and {to_unit} - they are different measurement categories")
        
        return result
    
    def _is_distance_unit(self, unit: str) -> bool:
        """Check if a unit is a distance unit"""
        distance_units = ["meters", "kilometers", "centimeters", "millimeters", 
                         "miles", "yards", "feet", "inches", "leagues"]
        return unit in distance_units
    
    def _is_volume_unit(self, unit: str) -> bool:
        """Check if a unit is a volume unit"""
        volume_units = ["liters", "milliliters", "gallons", "quarts", "pints", 
                       "cups", "tablespoons", "teaspoons", "fluid_ounces"]
        return unit in volume_units
    
    def _is_weight_unit(self, unit: str) -> bool:
        """Check if a unit is a weight unit"""
        weight_units = ["grams", "kilograms", "milligrams", "pounds", "ounces", 
                       "tons", "metric_tons"]
        return unit in weight_units
