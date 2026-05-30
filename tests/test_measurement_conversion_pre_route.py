"""Unit tests for MeasurementConversionCommand pre-route."""

import pytest

from commands.measurement_conversion_command import MeasurementConversionCommand


@pytest.fixture
def cmd():
    return MeasurementConversionCommand()


class TestPreRouteConvert:
    @pytest.mark.parametrize("phrase,expected", [
        ("convert 5 miles to kilometers", {"value": 5.0, "from_unit": "miles", "to_unit": "kilometers"}),
        ("convert 12 inches to centimeters", {"value": 12.0, "from_unit": "inches", "to_unit": "centimeters"}),
        ("convert 10 pounds to kilograms", {"value": 10.0, "from_unit": "pounds", "to_unit": "kilograms"}),
        ("convert 350 fahrenheit to celsius", {"value": 350.0, "from_unit": "fahrenheit", "to_unit": "celsius"}),
        ("convert 2 liters into gallons", {"value": 2.0, "from_unit": "liters", "to_unit": "gallons"}),
        ("convert 350 F to C", {"value": 350.0, "from_unit": "fahrenheit", "to_unit": "celsius"}),
        ("convert 5 km to miles", {"value": 5.0, "from_unit": "kilometers", "to_unit": "miles"}),
    ])
    def test_convert(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == expected


class TestPreRouteHowMany:
    @pytest.mark.parametrize("phrase,expected", [
        ("how many feet in a mile", {"value": 1.0, "from_unit": "miles", "to_unit": "feet"}),
        ("how many cups in a gallon", {"value": 1.0, "from_unit": "gallons", "to_unit": "cups"}),
        ("how many tablespoons in a cup", {"value": 1.0, "from_unit": "cups", "to_unit": "tablespoons"}),
        ("how many grams in 3 ounces", {"value": 3.0, "from_unit": "ounces", "to_unit": "grams"}),
        ("how many teaspoons in a tablespoon", {"value": 1.0, "from_unit": "tablespoons", "to_unit": "teaspoons"}),
    ])
    def test_how_many(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == expected


class TestPreRouteWhatsIn:
    @pytest.mark.parametrize("phrase,expected", [
        ("what's 350 fahrenheit in celsius", {"value": 350.0, "from_unit": "fahrenheit", "to_unit": "celsius"}),
        ("what is 25 celsius in fahrenheit", {"value": 25.0, "from_unit": "celsius", "to_unit": "fahrenheit"}),
        ("what's 100 kilometers in miles", {"value": 100.0, "from_unit": "kilometers", "to_unit": "miles"}),
    ])
    def test_whats_in(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == expected


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "how many bytes in a gigabyte",     # not a supported unit
        "convert me to imperial",           # bogus
        "convert 5 miles",                  # missing target
        "tell me a joke",
        "what time is it",
        "what is the weather in celsius",   # weather pref, not conversion
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "convert_measurement.convert",
            "convert_measurement.how_many",
            "convert_measurement.whats_in",
        }
