import sys
import time
import uuid
import json

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import DateContext
from core.request_information import RequestInformation
from services.chunked_command_response_service import ChunkedCommandResponseService
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.tool_result_formatter import format_tool_result, format_tool_error


def main():
    
    # Test utterances for different commands
    test_utterances = [
        # Calendar commands
        "what's on my calendar this week?",
        "show me my schedule for tomorrow",
        "what appointments do I have coming up?",
        
        # Weather commands
        "how hot is it outside?",
        "what's the weather forecast for this weekend?",
        "is it going to rain today?",
        
        # Joke commands
        "tell me a joke about programming",
        "make me laugh with something funny",
        
        # Knowledge commands
        "who was Albert Einstein?",
        "explain how photosynthesis works",
        
        # Story commands
        "tell me a story about a brave knight",
        "continue the story",
        "end the story"
    ]
    
    if len(sys.argv) < 2:
        print("=== JARVIS COMMAND TESTING ===")
        print("Usage: python test_command.py \"Your command text\"")
        print("\nOr test with predefined utterances:")
        for i, utterance in enumerate(test_utterances, 1):
            print(f"{i:2d}. {utterance}")
        print("\nExample: python test_command.py \"what's on my calendar this week?\"")
        sys.exit(1)

    user_input = " ".join(sys.argv[1:])
    print(f"=== TESTING COMMAND: {user_input} ===")

    discovery_service = get_command_discovery_service()
    discovery_service.refresh_now()

    # Call the client
    try:
        jcc_api_url = Config.get("jarvis_command_center_api_url")
        print(f"JCC API URL: {jcc_api_url}")
        jcc_client = JarvisCommandCenterClient(jcc_api_url)
        commands = discovery_service.get_all_commands()
        
        # Start conversation to warm up LLM (simulating wake word detection)
        conversation_id = str(uuid.uuid4())
        print(f"🔔 Starting conversation session: {conversation_id}")
        start_time = time.perf_counter()
        
        if jcc_client.start_conversation(conversation_id, commands, None):
            print("✅ Conversation started successfully, warming up LLM...")
            print("⏳ Simulating 3-second wait for verbal request...")
            time.sleep(2)  # Simulate waiting for actual verbal request
            start_time = time.perf_counter()
            print("🎤 Processing verbal request...")
        else:
            print("⚠️  Failed to start conversation, continuing anyway...")
        
        # Send the command with conversation ID
        print("🔍 Starting command discovery...")
        discovery_start = time.perf_counter()
        response = jcc_client.send_command(user_input, conversation_id)
        discovery_time = time.perf_counter() - discovery_start
        print(f"⏱️  Command discovery completed in {discovery_time:.3f} seconds")
        
        if not response:
            print("❌ No response received from Command Center")
            return
        
        print(f"\n=== COMMAND DISCOVERY RESPONSE ===")
        print(f"Stop Reason: {response.stop_reason}")
        print(f"Assistant Message: {response.assistant_message}")
        
        # Check if this is a tool calling response
        if hasattr(response, 'tool_calls') and response.tool_calls:
            print(f"🔧 Tool calling response - {len(response.tool_calls)} tool(s) to execute")
            commands_to_execute = []
            for tool_call in response.tool_calls:
                tool_name = tool_call.function.name
                arguments = tool_call.function.get_arguments_dict()
                commands_to_execute.append({
                    "command_name": tool_name,
                    "parameters": arguments,
                    "tool_call_id": tool_call.id
                })
        elif hasattr(response, 'commands') and response.commands:
            print(f"📦 Legacy command response - {len(response.commands)} command(s)")
            commands_to_execute = response.commands
        else:
            print(f"ℹ️  No commands or tool calls to execute")
            if response.assistant_message:
                print(f"Final message: {response.assistant_message}")
            return
        
        for i, cmd in enumerate(commands_to_execute, 1):
            print(f"\n--- EXECUTING COMMAND {i} ---")
            print(f"Full command object: {cmd}")
            print(f"Command name: {cmd.get('command_name', 'Unknown')}")
            print(f"Parameters: {cmd.get('parameters', {})}")
            
            command_name = cmd.get("command_name")
            if not command_name:
                print(f"❌ Command name is missing from command {i}")
                print(f"   Available keys: {list(cmd.keys())}")
                continue
                
            params = cmd.get("parameters", {}) or {}
            
            if command_name not in commands:
                print(f"❌ Command '{command_name}' not found in available commands")
                print(f"   Available commands: {list(commands.keys())}")
                continue
                
            command_to_execute = commands[command_name]
            
            # Create RequestInformation object
            request_info = RequestInformation(
                voice_command=user_input,
                conversation_id=conversation_id
            )
            
            # Execute command with new structure
            command_response = command_to_execute.execute(request_info, **params)
            
            print(f"\n=== COMMAND RESPONSE (Sent to Server) ===")
            print(f"Type: {type(command_response).__name__}")
            print(f"Success: {command_response.success}")
            print(f"Wait for Input: {command_response.wait_for_input}")
            print(f"Error Details: {command_response.error_details or 'None'}")
            print(f"Is Chunked Response: {command_response.is_chunked_response}")
            if command_response.chunk_session_id:
                print(f"Chunk Session ID: {command_response.chunk_session_id}")
            
            if command_response.context_data:
                print(f"Context Data Keys: {list(command_response.context_data.keys())}")
                # Show some sample context data
                for key, value in list(command_response.context_data.items())[:5]:
                    if isinstance(value, (str, int, float, bool)):
                        print(f"  {key}: {value}")
                    elif isinstance(value, list):
                        print(f"  {key}: [{len(value)} items]")
                    elif isinstance(value, dict):
                        print(f"  {key}: {{{len(value)} keys}}")
                    else:
                        print(f"  {key}: {type(value).__name__}")
            
            # Format the tool result to send back to server
            tool_call_id = cmd.get("tool_call_id", "unknown")
            if command_response.success:
                tool_result = format_tool_result(tool_call_id, command_response)
            else:
                tool_result = format_tool_error(tool_call_id, 
                                               command_response.error_details or "Unknown error")
            
            print(f"\n=== TOOL RESULT (Formatted for API) ===")
            print(json.dumps(tool_result, indent=2, default=str))
            
            # Send tool results back to server and get final response
            print(f"\n📤 Sending tool results back to server...")
            server_response = jcc_client.send_tool_results(conversation_id, [tool_result])
            
            if server_response:
                print(f"\n=== SERVER RESPONSE (What will be spoken) ===")
                print(f"Stop Reason: {server_response.stop_reason}")
                if server_response.assistant_message:
                    print(f"🔊 Assistant Message: {server_response.assistant_message}")
                else:
                    print(f"🔊 Assistant Message: (None)")
                
                # Check if there are more tool calls (multi-turn conversation)
                if hasattr(server_response, 'tool_calls') and server_response.tool_calls:
                    print(f"⚠️  Server requested {len(server_response.tool_calls)} more tool calls")
                    print(f"   (This test script only handles single-turn for now)")
                
                # Check if validation is needed
                if hasattr(server_response, 'validation_request') and server_response.validation_request:
                    print(f"⚠️  Server requested user validation")
                    print(f"   Question: {server_response.validation_request.question}")
            else:
                print(f"❌ No response received from server")
            
            # Handle chunked responses by speaking the content
            if command_response.is_chunked_response and command_response.chunk_session_id:
                print(f"\n=== HANDLING CHUNKED RESPONSE ===")
                print(f"Session ID: {command_response.chunk_session_id}")
                
                # Initialize the chunked service
                chunked_service = ChunkedCommandResponseService()
                
                try:
                    # Speak the current content
                    print("📢 Speaking chunked content...")
                    spoken_content = chunked_service.speak_session_until_caught_up(command_response.chunk_session_id)
                    
                    if spoken_content:
                        print(f"✅ Spoke content: {len(spoken_content)} characters")
                        print(f"   Content preview: '{spoken_content[:100]}{'...' if len(spoken_content) > 100 else ''}'")
                    else:
                        print("ℹ️  No new content to speak")
                    
                    # Show session status
                    status = chunked_service.get_session_status(command_response.chunk_session_id)
                    if status:
                        print(f"📊 Session Status:")
                        print(f"   Total Content: {status['total_content_length']} chars")
                        print(f"   Remaining: {status['remaining_content_length']} chars")
                        print(f"   Is Caught Up: {status['is_caught_up']}")
                    
                except Exception as e:
                    print(f"❌ Error handling chunked response: {e}")
                    import traceback
                    traceback.print_exc()
        
        end_time = time.perf_counter()
        elapsed = end_time - start_time
        print(f"\n=== EXECUTION SUMMARY ===")
        print(f"Total roundtrip time: {elapsed:.2f} seconds")
        
    except Exception as e:
        print(f"Error executing command: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()