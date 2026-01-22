from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime, date, time, timedelta


class IJarvisParameter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Parameter name (e.g. 'room', 'brightness')"""
        pass

    @property
    @abstractmethod
    def param_type(self) -> str:
        """Parameter type (e.g. 'str', 'int', 'bool')"""
        pass

    @property
    @abstractmethod
    def description(self) -> Optional[str]:
        """Human-readable description of this parameter"""
        pass

    @property
    def required(self) -> bool:
        """Whether this parameter is required (default: True)"""
        return True

    @property
    def default_value(self) -> Optional[Any]:
        """Default value for this parameter (if not required)"""
        return None

    @property
    def validation_function(self) -> Optional[Callable[[Any], bool]]:
        """Optional validation function that returns True if value is valid"""
        return None

    @property
    def validation_error_message(self) -> str:
        """Error message to show when validation fails"""
        return f"Invalid value for parameter '{self.name}'"

    @property
    def enum_values(self) -> Optional[List[str]]:
        """Optional list of allowed values if this parameter is an enum"""
        return None

    def validate(self, value: Any) -> tuple[bool, Optional[str]]:
        """
        Validate a parameter value
        
        Args:
            value: The value to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Type validation
        if not self._validate_type(value):
            return False, f"Parameter '{self.name}' must be of type {self.param_type}"
        
        # Custom validation
        if self.validation_function and not self.validation_function(value):
            return False, self.validation_error_message
            
        return True, None

    def _validate_type(self, value: Any) -> bool:
        """Basic type validation"""
        if value is None:
            return not self.required
            
        # Handle type aliases and map to actual types
        type_aliases = {
            'str': str, 'string': str,
            'int': int, 'integer': int,
            'float': float,
            'bool': bool, 'boolean': bool,
            'list': list, 'array': list,
            'dict': dict,
            'datetime': datetime,
            'date': date,
            'time': time,
            'timedelta': timedelta
        }

        # Handle array type grammars like array<datetime>, array[datetime], datetime[]
        if isinstance(self.param_type, str):
            param_type = self.param_type.strip()
            if param_type.startswith("array<") and param_type.endswith(">"):
                return isinstance(value, list)
            if param_type.startswith("array[") and param_type.endswith("]"):
                return isinstance(value, list)
            if param_type.endswith("[]"):
                return isinstance(value, list)
        
        expected_type = type_aliases.get(self.param_type)
        if expected_type is None:
            return True  # Unknown type, assume valid
            
        return isinstance(value, expected_type)

    def to_dict(self) -> Dict[str, Any]:
        """Convert parameter to dictionary for JSON serialization"""
        return {
            "name": self.name,
            "type": self.param_type,
            "description": self.description,
            "required": self.required,
            "default_value": self.default_value,
            "enum_values": self.enum_values
        }

class JarvisParameter(IJarvisParameter):

    def __init__(self, name: str, param_type: str, required: bool = False, description: Optional[str]=None, default: Optional[str]=None, enum_values: Optional[List[str]]=None):
        # Validate that param_type is allowed
        allowed_types = {
            # Primitive types
            'str', 'string', 'int', 'integer', 'float', 'bool', 'boolean', 'list', 'array', 'dict',
            # Datetime types
            'datetime', 'date', 'time', 'timedelta',
            # Array types (angle and bracket syntax)
            'array<datetime>', 'array<date>', 'array<time>', 'array<timedelta>',
            'array<string>', 'array<int>', 'array<float>', 'array<bool>',
            'array[datetime]', 'array[date]', 'array[time]', 'array[timedelta]',
            # Shorthand array aliases
            'datetime[]', 'date[]', 'time[]', 'timedelta[]'
        }
        
        if param_type not in allowed_types:
            raise ValueError(
                f"Parameter type '{param_type}' is not allowed. "
                f"Only primitive types and datetime types are supported. "
                f"Allowed types: {', '.join(sorted(allowed_types))}"
            )
        
        self._name = name
        self._param_type = param_type
        self._required = required
        self._description = description
        self._default = default
        self._enum_values = enum_values

    @property
    def name(self) -> str:
        return self._name

    @property
    def param_type(self) -> str:
        return self._param_type

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def required(self) -> bool:
        return self._required

    @property
    def default_value(self) -> Optional[str]:
        return self._default

    @property
    def enum_values(self) -> Optional[List[str]]:
        return self._enum_values

