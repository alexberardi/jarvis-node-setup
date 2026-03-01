import sys
import time
import uuid
import json
from pathlib import Path

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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

    # Start agent scheduler to fetch HA device context
    try:
        from services.agent_scheduler_service import initialize_agent_scheduler
        agent_scheduler = initialize_agent_scheduler()
        print("⏳ Waiting for agent scheduler to fetch device context...")
        time.sleep(3)
        print("✅ Agent scheduler started")
    except Exception as e:
        print(f"⚠️  Failed to start agent scheduler: {e} (HA device tests may fail)")

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
        
        # Multi-turn tool execution loop
        current_response = response
        turn = 0
        max_turns = 10

        while turn < max_turns:
            turn += 1

            # Extract tool calls from current response
            if hasattr(current_response, 'tool_calls') and current_response.tool_calls:
                print(f"\n🔧 Turn {turn}: {len(current_response.tool_calls)} tool call(s) to execute")
                commands_to_execute = []
                for tool_call in current_response.tool_calls:
                    tool_name = tool_call.function.name
                    arguments = tool_call.function.get_arguments_dict()
                    commands_to_execute.append({
                        "command_name": tool_name,
                        "parameters": arguments,
                        "tool_call_id": tool_call.id
                    })
            elif hasattr(current_response, 'commands') and current_response.commands:
                print(f"\n📦 Turn {turn}: Legacy command response - {len(current_response.commands)} command(s)")
                commands_to_execute = current_response.commands
            else:
                # No more tool calls — print final message and exit loop
                if current_response.assistant_message:
                    print(f"\n🔊 Final message: {current_response.assistant_message}")
                break

            # Execute all tool calls and collect results
            tool_results = []
            for i, cmd in enumerate(commands_to_execute, 1):
                print(f"\n--- EXECUTING COMMAND {i} (Turn {turn}) ---")
                print(f"Command: {cmd.get('command_name', 'Unknown')}")
                print(f"Parameters: {cmd.get('parameters', {})}")

                command_name = cmd.get("command_name")
                if not command_name:
                    print(f"❌ Command name is missing")
                    continue

                params = cmd.get("parameters", {}) or {}

                if command_name not in commands:
                    print(f"❌ Command '{command_name}' not found")
                    print(f"   Available: {list(commands.keys())}")
                    continue

                command_to_execute = commands[command_name]
                request_info = RequestInformation(
                    voice_command=user_input,
                    conversation_id=conversation_id
                )
                command_response = command_to_execute.execute(request_info, **params)

                print(f"  Success: {command_response.success}")
                if command_response.context_data:
                    msg = command_response.context_data.get("message", "")
                    if msg:
                        print(f"  Result: {msg}")
                if command_response.error_details:
                    print(f"  Error: {command_response.error_details}")

                # Format tool result
                tool_call_id = cmd.get("tool_call_id", "unknown")
                if command_response.success:
                    tool_result = format_tool_result(tool_call_id, command_response)
                else:
                    tool_result = format_tool_error(
                        tool_call_id,
                        command_response.error_details or "Unknown error"
                    )
                tool_results.append(tool_result)

                # Handle chunked responses
                if command_response.is_chunked_response and command_response.chunk_session_id:
                    chunked_service = ChunkedCommandResponseService()
                    try:
                        spoken_content = chunked_service.speak_session_until_caught_up(
                            command_response.chunk_session_id
                        )
                        if spoken_content:
                            print(f"  📢 Spoke {len(spoken_content)} chars")
                    except Exception as e:
                        print(f"  ❌ Chunked response error: {e}")

            if not tool_results:
                print("❌ No tool results to send")
                break

            # Send all tool results back at once
            print(f"\n📤 Sending {len(tool_results)} tool result(s) back to server...")
            server_response = jcc_client.send_tool_results(conversation_id, tool_results)

            if not server_response:
                print("❌ No response received from server")
                break

            print(f"Stop Reason: {server_response.stop_reason}")
            if server_response.assistant_message:
                print(f"🔊 Assistant: {server_response.assistant_message}")

            # Check for validation requests
            if hasattr(server_response, 'validation_request') and server_response.validation_request:
                print(f"⚠️  Validation requested: {server_response.validation_request.question}")
                break

            # Continue loop if server wants more tool calls
            if hasattr(server_response, 'tool_calls') and server_response.tool_calls:
                current_response = server_response
                continue
            else:
                break

        if turn >= max_turns:
            print(f"⚠️  Reached max turns ({max_turns})")
        
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