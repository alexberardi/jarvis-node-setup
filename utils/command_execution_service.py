import uuid
from typing import Dict, Any, List, Optional, Callable

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import ToolCallingResponse, ToolCall, ValidationRequest
from core.helpers import get_tts_provider
from core.request_information import RequestInformation
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.service_discovery import get_command_center_url
from utils.tool_result_formatter import format_tool_result, format_tool_error


class CommandExecutionService:
    def __init__(self):
        self.command_center_url = get_command_center_url()
        self.node_id = Config.get_str("node_id")
        self.room = Config.get_str("room")
        self.command_discovery = get_command_discovery_service()
        self.client = JarvisCommandCenterClient(self.command_center_url)
        # Force initial discovery
        self.command_discovery.refresh_now()
    
    def register_tools_for_conversation(self, conversation_id: str) -> bool:
        """
        Register available client-side tools with the Command Center for a conversation
        
        Args:
            conversation_id: The conversation identifier
            
        Returns:
            True if successful, False otherwise
        """
        commands = self.command_discovery.get_all_commands()
        
        if not commands:
            print("⚠️  No commands available to register")
            return False
        
        print(f"📋 Registering {len(commands)} tools for conversation {conversation_id}")
        
        try:
            # Get date context
            date_context = self.client.get_date_context()
            
            # Start conversation with available commands
            success = self.client.start_conversation(conversation_id, commands, date_context)
            
            if success:
                print(f"✅ Successfully registered {len(commands)} tools")
            else:
                print(f"❌ Failed to register tools")
            
            return success
            
        except Exception as e:
            print(f"❌ Error registering tools: {e}")
            return False

    def process_voice_command(
        self, 
        voice_command: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
        register_tools: bool = True
    ) -> Dict[str, Any]:
        """
        Process a voice command through the tool calling pipeline:
        1. Optionally register available tools with the server
        2. Send to Jarvis Command Center for LLM processing
        3. Loop on response stop_reason:
           - tool_call: Execute tools locally and send results back
           - validation: Ask user for clarification and send response back
           - stop/complete: Return final message
        
        Args:
            voice_command: The transcribed voice command
            validation_handler: Optional callback to handle validation requests
                               If None, default behavior is used
            register_tools: Whether to register available tools before processing (default: True)
            
        Returns:
            Execution result dictionary with success, message, and conversation_id
        """
        conversation_id = self._generate_conversation_id()
        
        print(f"🆔 Starting conversation: {conversation_id}")
        print(f"📡 Processing: {voice_command}")
        
        try:
            # Register available tools if requested
            if register_tools:
                self.register_tools_for_conversation(conversation_id)
            
            # Initial request to Command Center
            response = self.client.send_command(voice_command, conversation_id)
            
            if not response:
                return self._handle_error("Failed to communicate with Command Center", conversation_id)
            
            # Loop until conversation is complete
            max_iterations = 10  # Safety limit to prevent infinite loops
            iteration = 0
            
            while not response.is_final() and iteration < max_iterations:
                iteration += 1
                print(f"🔄 Iteration {iteration}: stop_reason = {response.stop_reason}")
                
                if response.requires_tool_execution():
                    # Execute requested tools
                    print(f"🔧 Executing {len(response.tool_calls)} tool(s)")
                    tool_results = self._execute_tools(response.tool_calls, conversation_id)
                    
                    # Send results back to continue conversation
                    response = self.client.send_tool_results(conversation_id, tool_results)
                    
                    if not response:
                        return self._handle_error("Failed to send tool results", conversation_id)
                
                elif response.requires_validation():
                    # Handle validation/clarification request
                    print(f"❓ Validation required: {response.validation_request.question}")
                    
                    if validation_handler:
                        user_response = validation_handler(response.validation_request)
                    else:
                        user_response = self._default_validation_handler(response.validation_request)
                    
                    # Send validation response back
                    response = self.client.send_validation_response(
                        conversation_id,
                        response.validation_request,
                        user_response
                    )
                    
                    if not response:
                        return self._handle_error("Failed to send validation response", conversation_id)
                
                else:
                    # Unknown stop_reason, treat as error
                    return self._handle_error(f"Unknown stop_reason: {response.stop_reason}", conversation_id)
            
            if iteration >= max_iterations:
                return self._handle_error("Conversation exceeded maximum iterations", conversation_id)
            
            # Final response
            final_message = response.assistant_message or "Task completed."
            print(f"✅ Conversation complete: {final_message}")
            
            return {
                "success": True,
                "message": final_message,
                "conversation_id": conversation_id
            }
            
        except Exception as e:
            print(f"❌ Error processing command: {e}")
            return self._handle_error(f"Error processing command: {str(e)}", conversation_id)

    def _execute_tools(self, tool_calls: List[ToolCall], conversation_id: str) -> List[Dict[str, Any]]:
        """
        Execute client-side tools and return formatted results
        
        Args:
            tool_calls: List of tool calls to execute
            conversation_id: Current conversation ID
            
        Returns:
            List of formatted tool results in format: [{"tool_call_id": "...", "output": {...}}]
        """
        results = []
        
        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            print(f"  🔨 Executing: {tool_name} (id: {tool_call.id})")
            
            try:
                # Get the command instance
                command = self.command_discovery.get_command(tool_name)
                
                if not command:
                    error_msg = f"Unknown tool: {tool_name}"
                    print(f"    ❌ {error_msg}")
                    results.append(format_tool_error(tool_call.id, error_msg))
                    continue
                
                # Parse arguments from JSON string
                arguments = tool_call.function.get_arguments_dict()
                
                # Create request information
                request_info = RequestInformation(
                    voice_command=f"Tool call: {tool_name}",
                    conversation_id=conversation_id,
                    is_validation_response=False
                )
                
                # Execute the command with the provided arguments
                command_response = command.execute(request_info, **arguments)
                
                # Format the result
                result = format_tool_result(tool_call.id, command_response)
                results.append(result)
                
                print(f"    ✅ Success: Tool executed successfully")
                
            except Exception as e:
                error_msg = str(e)
                print(f"    ❌ Error: {error_msg}")
                results.append(format_tool_error(tool_call.id, error_msg))
        
        return results

    def _default_validation_handler(self, validation: ValidationRequest) -> str:
        """
        Default validation handler - placeholder for now
        
        In practice, this should prompt the user via TTS and listen for their response.
        For now, we'll return a simple error message.
        
        Args:
            validation: The validation request
            
        Returns:
            User's response (or error message)
        """
        print(f"⚠️  Default validation handler called - this should be overridden")
        print(f"    Question: {validation.question}")
        if validation.options:
            print(f"    Options: {', '.join(validation.options)}")
        
        # For now, return a message indicating validation is not supported
        return "I'm not sure - please try rephrasing your request."

    def _generate_conversation_id(self) -> str:
        """Generate unique conversation ID for each voice interaction"""
        return str(uuid.uuid4())

    def _handle_error(self, message: str, conversation_id: str) -> Dict[str, Any]:
        """Handle general errors"""
        return {
            "success": False,
            "message": message,
            "conversation_id": conversation_id
        }

    def speak_result(self, result: Dict[str, Any]):
        """Speak the result of command execution"""
        tts_provider = get_tts_provider()
        message = result.get("message", "An error occurred")
        tts_provider.speak(False, message)
