from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

from exceptions.missing_secrets_error import MissingSecretsError
from services.secret_service import get_secret_value
from .ijarvis_parameter import IJarvisParameter
from .ijarvis_secret import IJarvisSecret
from .request_information import RequestInformation
from .command_response import CommandResponse
from clients.responses.jarvis_command_center import DateContext

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
    def generate_examples(self, date_context: DateContext) -> str:
        """Generate example utterances and how they get parsed into parameters using date context"""
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

    def get_command_schema(self, date_context: DateContext) -> Dict[str, Any]:
        """Generate the command schema for the LLM"""
        return {
            "command_name": self.command_name,
            "description": self.description,
            "example": self.generate_examples(date_context),
            "keywords": self.keywords,
            "parameters": [param.to_dict() for param in self.parameters]
        }

    def validate_secrets(self):
        missing = []
        for secret in self.required_secrets:
            if not get_secret_value(secret.key, secret.scope) :
                missing.append(secret.key)

        if missing:
            raise MissingSecretsError(missing)


