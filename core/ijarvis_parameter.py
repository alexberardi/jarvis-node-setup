from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable


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
    def description(self) -> str:
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
            
        type_mapping = {
            'str': str,
            'int': int,
            'float': float,
            'bool': bool,
            'list': list,
            'dict': dict
        }
        
        expected_type = type_mapping.get(self.param_type)
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
            "default_value": self.default_value
        } 