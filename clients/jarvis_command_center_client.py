from typing import Any, Type, TypeVar, Optional

from pydantic import BaseModel

from core.ijarvis_command import IJarvisCommand
from .responses.jarvis_command_center import DateContext
from .rest_client import RestClient
from utils.config_loader import Config
from utils.timezone_util import get_user_timezone

T = TypeVar("T", bound=BaseModel)

class JarvisCommandCenterClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
    
    def get_date_context(self) -> Optional[DateContext]:
        """Get the current date context from the server"""
        try:
            timezone = get_user_timezone()
            response = RestClient.get(f"{self.base_url}/api/v0/generate/date-context?timezone={timezone}")
            
            if not response:
                print(f"[JarvisClient] No response from date-context endpoint")
                return None
            
            # Parse the response into a DateContext object
            try:
                date_context = DateContext.model_validate(response)
                print(f"[JarvisClient] Successfully fetched date context for timezone: {date_context.timezone.user_timezone}")
                return date_context
            except Exception as parse_error:
                print(f"[JarvisClient] Failed to parse date context response: {parse_error}")
                print(f"[JarvisClient] Raw response: {response}")
                return None
            
        except Exception as e:
            print(f"[JarvisClient] Failed to get date context: {e}")
            return None

    def send_command(self, tts: str, conversation_id: str = None) -> dict[str, Any] | None:
        node_context = {
            "node_id": Config.get("node_id"),
            "room": Config.get("room"),
            "timezone": get_user_timezone()
        }

        payload = {
            "voice_command": tts,
            "node_context": node_context
        }
        
        # Add conversation_id if provided
        if conversation_id:
            payload["conversation_id"] = conversation_id
            
        print(payload)

        response = RestClient.post(f"{self.base_url}/api/v0/voice/command", timeout=30, data=payload)

        return response

    def chat(self, message: str, model: Type[T]) -> Optional[T]:
        response = RestClient.post(f"{self.base_url}/api/v0/chat", {
            "messages": [
                {"role": "system", "content": message}
            ]
        })
        print(f"Chat response: {response}")

        if not response:
            return None

        try:
            content = response["choices"][0]["message"]["content"]
            print(content)
            return model.model_validate_json(content)
        except (KeyError, ValueError, TypeError) as e:
            print(f"[JarvisClient] Failed to parse LLM response: {e}")
            
            # Enhanced fallback: try to extract JSON from mixed content
            try:
                if isinstance(content, str):
                    # Try to find JSON in the content
                    json_start = content.find('{')
                    json_end = content.rfind('}')
                    
                    if json_start != -1 and json_end != -1 and json_end > json_start:
                        # Extract just the JSON portion
                        json_content = content[json_start:json_end + 1]
                        print(f"[JarvisClient] Extracted JSON from mixed content: {json_content}")
                        return model.model_validate_json(json_content)
                    elif not content.strip().startswith('{'):
                        # If no JSON found, wrap plain text in the expected JSON format
                        wrapped_content = f'{{"response": "{content.strip()}"}}'
                        print(f"[JarvisClient] Attempting fallback JSON wrapping: {wrapped_content}")
                        return model.model_validate_json(wrapped_content)
            except Exception as fallback_error:
                print(f"[JarvisClient] Enhanced fallback parsing also failed: {fallback_error}")
            
            return None

    def lightweight_chat(self, message: str, model: Type[T]) -> Optional[T]:
        response = RestClient.post(f"{self.base_url}/api/v0/lightweight/chat", {
            "messages": [
                {"role": "system", "content": message}
            ]
        })

        if not response:
            return None

        try:
            content = response["choices"][0]["message"]["content"]
            print(content)
            return model.model_validate_json(content)
        except (KeyError, ValueError, TypeError) as e:
            print(f"[JarvisClient] Failed to parse LLM response: {e}")
            
            # Enhanced fallback: try to extract JSON from mixed content
            try:
                if isinstance(content, str):
                    # Try to find JSON in the content
                    json_start = content.find('{')
                    json_end = content.rfind('}')
                    
                    if json_start != -1 and json_end != -1 and json_end > json_start:
                        # Extract just the JSON portion
                        json_content = content[json_start:json_end + 1]
                        print(f"[JarvisClient] Extracted JSON from mixed content: {json_content}")
                        return model.model_validate_json(json_content)
                    elif not content.strip().startswith('{'):
                        # If no JSON found, wrap plain text in the expected JSON format
                        wrapped_content = f'{{"response": "{content.strip()}"}}'
                        print(f"[JarvisClient] Attempting fallback JSON wrapping: {wrapped_content}")
                        return model.model_validate_json(wrapped_content)
            except Exception as fallback_error:
                print(f"[JarvisClient] Enhanced fallback parsing also failed: {fallback_error}")
            
            return None

    def start_conversation(self, conversation_id: str, commands: dict[str, IJarvisCommand], date_context: Optional[DateContext] = None) -> bool:
        """
        Start a conversation session to warm up the LLM.
        
        Args:
            conversation_id: UUID for the conversation session
            commands: Dictionary of available commands to send to the command center
            date_context: Optional date context to use for command examples
            
        Returns:
            True if successful, False otherwise
        """
        # Get date context if not provided
        if date_context is None:
            date_context = self.get_date_context()
        
        node_context = {
            "node_id": Config.get("node_id"),
            "room": Config.get("room"),
            "timezone": get_user_timezone()
        }
        
        # Build available_commands array
        available_commands = []
        for cmd in commands.values():
            print(cmd.command_name)
            available_commands.append({
                "command_name": cmd.command_name,
                "description": cmd.description,
                "keywords": cmd.keywords,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.param_type,
                        **({"description": p.description} if p.description is not None else {}),
                        **({"enum_values": p.enum_values} if p.enum_values is not None else {}),
                        "required": p.required
                    }
                    for p in cmd.parameters
                ],
                "example": cmd.generate_examples(date_context)
            })
        
        payload = {
            "node_context": node_context,
            "conversation_id": conversation_id,
            "available_commands": available_commands
        }
        
        try:
            response = RestClient.post(f"{self.base_url}/api/v0/conversation/start", timeout=10, data=payload)
            return response is not None
        except Exception as e:
            print(f"[JarvisClient] Failed to start conversation: {e}")
            return False



