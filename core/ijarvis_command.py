from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from exceptions.missing_secrets_error import MissingSecretsError
from services.secret_service import get_secret_value
from .ijarvis_parameter import IJarvisParameter
from .ijarvis_secret import IJarvisSecret
from .request_information import RequestInformation
from .command_response import CommandResponse
from clients.responses.jarvis_command_center import DateContext


@dataclass
class CommandExample:
    """Represents a voice command example with expected parameters"""
    voice_command: str
    expected_parameters: Dict[str, Any]
    is_primary: bool = False

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
            if not get_secret_value(secret.key, secret.scope):
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
    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate example utterances with expected parameters using date context"""
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
    def critical_rules(self) -> List[str]:
        """Optional list of critical rules that must be followed for this command"""
        return []

    @abstractmethod
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """
        Execute the command with request information and parameters
        
        Args:
            request_info: Information about the request from JCC
            **kwargs: Additional parameters for the command
            
        Returns:
            CommandResponse object with:
            - speak_message: What Jarvis will say to the user
            - wait_for_input: Whether to wait for follow-up questions
            - context_data: Data for follow-up context
            - success: Whether the command succeeded
            - error_details: Any error information
        """
        pass

    def _validate_examples(self, examples: List[CommandExample]) -> None:
        """Validate that examples follow the rules"""
        primary_count = sum(1 for ex in examples if ex.is_primary)
        if primary_count > 1:
            raise ValueError(f"Command '{self.command_name}' has {primary_count} primary examples. Only 0 or 1 allowed.")
    
    def get_command_schema(self, date_context: DateContext) -> Dict[str, Any]:
        """Generate the command schema for the LLM"""
        examples = self.generate_examples(date_context)
        self._validate_examples(examples)
        
        schema = {
            "command_name": self.command_name,
            "description": self.description,
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
        
        # Add critical rules if they exist
        if self.critical_rules:
            schema["critical_rules"] = self.critical_rules
            
        return schema
    
    def get_primary_example(self, date_context: DateContext) -> CommandExample:
        """Get the primary example for command inference (or first if none marked primary)"""
        examples = self.generate_examples(date_context)
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
            if not get_secret_value(secret.key, secret.scope) :
                missing.append(secret.key)

        if missing:
            raise MissingSecretsError(missing)


