from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from .ijarvis_parameter import IJarvisParameter


class IJarvisCommand(ABC):
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

    @property
    @abstractmethod
    def parameters(self) -> List[IJarvisParameter]:
        """List of parameters this command accepts"""
        pass

    @property
    @abstractmethod
    def keywords(self) -> List[str]:
        """List of keywords that can be used to identify this command (for fuzzy matching)"""
        pass

    @abstractmethod
    def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the command with the provided parameters
        
        Args:
            params: Dictionary mapping parameter names to their values
            
        Returns:
            Dictionary with execution results:
            {
                "success": bool,
                "message": str,
                "data": Optional[Any],
                "errors": Optional[Dict[str, Any]]
            }
        """
        pass

    def get_command_schema(self) -> Dict[str, Any]:
        """Generate the command schema for the LLM"""
        return {
            "command_name": self.command_name,
            "description": self.description,
            "keywords": self.keywords,
            "parameters": [param.to_dict() for param in self.parameters]
        } 