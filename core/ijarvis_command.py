from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from dataclasses import dataclass

from exceptions.missing_secrets_error import MissingSecretsError
from services.secret_service import get_secret_value
from .ijarvis_parameter import IJarvisParameter
from .ijarvis_secret import IJarvisSecret
from .request_information import RequestInformation
from .command_response import CommandResponse
from clients.responses.jarvis_command_center import DateContext

if TYPE_CHECKING:
    from .ijarvis_package import JarvisPackage


@dataclass
class CommandExample:
    """Represents a voice command example with expected parameters"""
    voice_command: str
    expected_parameters: Dict[str, Any]
    is_primary: bool = False


@dataclass
class CommandAntipattern:
    """Represents a command anti-pattern for tool disambiguation"""
    command_name: str
    description: str

class JarvisCommandBase(ABC):
    def execute(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """
        Execute the command with request information and parameters
        
        Args:
            request_info: Information about the request from JCC
            **kwargs: Additional parameters for the command
            
        Returns:
            CommandResponse object with execution results
        """
        self._validate_secrets()
        self._validate_params(kwargs)
        return self.run(request_info, **kwargs)

    def _validate_secrets(self):
        missing = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)
        if missing:
            raise MissingSecretsError(missing)

    def _validate_params(self, kwargs):
        missing = [
            p.name for p in self.parameters if p.required and kwargs.get(p.name) is None
        ]
        if missing:
            raise ValueError(f"Missing required params: {', '.join(missing)}")

    @abstractmethod
    def run(self, **kwargs):
        pass




class IJarvisCommand(JarvisCommandBase, ABC):
    @property
    @abstractmethod
    def command_name(self) -> str:
        """Unique identifier for this command (e.g. 'turn_on_lights', 'check_door_status')"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this command does"""
        pass

    @abstractmethod
    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        pass

    @abstractmethod
    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate larger, varied examples for adapter training"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> List[IJarvisParameter]:
        """List of parameters this command accepts"""
        pass

    @property
    @abstractmethod
    def required_secrets(self) -> List[IJarvisSecret]:
        pass

    @property
    @abstractmethod
    def keywords(self) -> List[str]:
        """List of keywords that can be used to identify this command (for fuzzy matching)"""
        pass

    @property
    def rules(self) -> List[str]:
        """Optional list of general rules for this command"""
        return []

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        """Optional list of anti-patterns that point to other commands"""
        return []

    @property
    def allow_direct_answer(self) -> bool:
        """Whether the model may respond directly without calling the tool."""
        return False

    @property
    def critical_rules(self) -> List[str]:
        """Optional list of critical rules that must be followed for this command"""
        return []

    @property
    def required_packages(self) -> List["JarvisPackage"]:
        """
        Python packages this command requires.

        Override to declare pip dependencies for this command.
        Packages are installed on first use and written to custom-requirements.txt.

        Returns:
            List of JarvisPackage declaring pip dependencies
        """
        return []

    def init_data(self) -> Dict[str, Any]:
        """
        Optional initialization hook. Called manually on first install.

        Override to sync data on first install:
        - Register devices with Command Center
        - Fetch initial state from external services
        - Set up integrations

        Returns:
            Dict with initialization results (for logging/display)

        Usage:
            python scripts/init_data.py --command <command_name>
        """
        return {"status": "no_init_required"}

    @abstractmethod
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """
        Execute the command with request information and parameters
        
        Args:
            request_info: Information about the request from JCC
            **kwargs: Additional parameters for the command
            
        Returns:
            CommandResponse object with:
            - context_data: Raw data for the server to use in generating response
            - success: Whether the command succeeded
            - error_details: Any error information
            - wait_for_input: Whether to wait for follow-up questions
        """
        pass

    def _validate_examples(self, examples: List[CommandExample]) -> None:
        """Validate that examples follow the rules"""
        primary_count = sum(1 for ex in examples if ex.is_primary)
        if primary_count > 1:
            raise ValueError(f"Command '{self.command_name}' has {primary_count} primary examples. Only 0 or 1 allowed.")
    
    def get_command_schema(self, date_context: DateContext, use_adapter_examples: bool = False) -> Dict[str, Any]:
        """Generate the command schema for the LLM"""
        examples = self.generate_adapter_examples() if use_adapter_examples else self.generate_prompt_examples()
        self._validate_examples(examples)
        
        schema = {
            "command_name": self.command_name,
            "description": self.description,
            "allow_direct_answer": self.allow_direct_answer,
            "examples": [
                {
                    "voice_command": ex.voice_command,
                    "expected_parameters": ex.expected_parameters,
                    "is_primary": ex.is_primary
                }
                for ex in examples
            ],
            "keywords": self.keywords,
            "parameters": [param.to_dict() for param in self.parameters]
        }
        
        # Add rules if they exist
        if self.rules:
            schema["rules"] = self.rules
        
        # Add antipatterns if they exist
        if self.antipatterns:
            schema["antipatterns"] = [
                {
                    "command_name": antipattern.command_name,
                    "description": antipattern.description
                }
                for antipattern in self.antipatterns
            ]

        # Add critical rules if they exist
        if self.critical_rules:
            schema["critical_rules"] = self.critical_rules
            
        return schema
    
    def get_primary_example(self, date_context: DateContext) -> CommandExample:
        """Get the primary example for command inference (or first if none marked primary)"""
        examples = self.generate_prompt_examples()
        self._validate_examples(examples)
        
        # Find primary example
        primary_examples = [ex for ex in examples if ex.is_primary]
        if primary_examples:
            return primary_examples[0]
        
        # If no primary, return first example
        if examples:
            return examples[0]
        
        raise ValueError(f"Command '{self.command_name}' has no examples")

    def validate_secrets(self):
        missing = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)

        if missing:
            raise MissingSecretsError(missing)
    
    def to_openai_tool_schema(self, date_context: DateContext) -> Dict[str, Any]:
        """
        Convert this command to OpenAI function/tool calling schema format
        
        Args:
            date_context: Date context for generating examples
            
        Returns:
            Dictionary in OpenAI tool schema format
        """
        examples = self.generate_prompt_examples()
        self._validate_examples(examples)


        # Map our parameter types to JSON Schema types
        type_mapping = {
            'str': 'string', 'string': 'string',
            'int': 'integer', 'integer': 'integer',
            'float': 'number',
            'bool': 'boolean', 'boolean': 'boolean',
            'list': 'array', 'array': 'array',
            'dict': 'object',
            'datetime': 'string',  # ISO datetime strings
            'date': 'string',  # ISO date strings
            'time': 'string',  # ISO time strings
            'array[datetime]': 'array',
            'array[date]': 'array',
            'array<datetime>': 'array',
            'array<date>': 'array',
            'datetime[]': 'array',
            'date[]': 'array',
        }
        
        properties = {}
        required_params = []
        
        for param in self.parameters:
            json_type = type_mapping.get(param.param_type, 'string')
            
            param_schema = {
                "type": json_type
            }
            
            if param.description:
                param_schema["description"] = param.description
            
            if param.enum_values:
                param_schema["enum"] = param.enum_values
            
            # Handle array types
            if param.param_type.startswith('array') or param.param_type.endswith('[]'):
                param_schema["type"] = "array"
                if 'datetime' in param.param_type:
                    param_schema["items"] = {"type": "string", "format": "date-time"}
                elif 'date' in param.param_type:
                    param_schema["items"] = {"type": "string", "format": "date"}
            
            properties[param.name] = param_schema
            
            if param.required:
                required_params.append(param.name)
        
        tool_schema = {
            "type": "function",
            "function": {
                "name": self.command_name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required_params
                }
            },
            "allow_direct_answer": self.allow_direct_answer,
            "keywords": self.keywords,
            # Include examples to help the server-side model during warmup
            "examples": [
                {
                    "voice_command": ex.voice_command,
                    "expected_parameters": ex.expected_parameters,
                    "is_primary": ex.is_primary
                } for ex in examples
            ]
        }

        if self.antipatterns:
            tool_schema["antipatterns"] = [
                {
                    "command_name": antipattern.command_name,
                    "description": antipattern.description
                }
                for antipattern in self.antipatterns
            ]

        return tool_schema


