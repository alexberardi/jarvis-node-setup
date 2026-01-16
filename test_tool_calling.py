#!/usr/bin/env python3
"""
Test script for tool calling functionality

This script tests the new tool calling architecture by:
1. Creating a conversation with registered tools
2. Sending a voice command
3. Handling tool calls and validation requests
4. Completing the conversation
"""

import sys
import uuid
import traceback
from utils.command_execution_service import CommandExecutionService
from clients.responses.jarvis_command_center import ValidationRequest


def simple_validation_handler(validation: ValidationRequest) -> str:
    """
    Simple validation handler for testing (non-interactive)
    
    In a real scenario, this would prompt the user via TTS and listen for response.
    For testing, we'll just return a reasonable default.
    """
    print(f"\n🔍 VALIDATION REQUEST:")
    print(f"   Question: {validation.question}")
    
    if validation.options:
        print(f"   Options: {', '.join(validation.options)}")
        # Return first option as default for testing
        return validation.options[0]
    
    # Return a default response
    return "I'm not sure"


def test_tool_calling():
    """Test the tool calling flow with a simple command"""
    
    print("=" * 80)
    print("TOOL CALLING TEST")
    print("=" * 80)
    
    # Create command execution service
    service = CommandExecutionService()
    
    # Test commands
    test_commands = [
        "What's the weather like?",
        "Tell me a joke",
        "What's 25 plus 37?",
        "Search the web for latest AI news"
    ]
    
    print(f"\n📋 Testing {len(test_commands)} commands with tool calling flow\n")
    
    for i, command in enumerate(test_commands, 1):
        print(f"\n{'='*80}")
        print(f"TEST {i}/{len(test_commands)}: {command}")
        print(f"{'='*80}\n")
        
        try:
            # Process the command with validation handler
            result = service.process_voice_command(
                command,
                validation_handler=simple_validation_handler,
                register_tools=True
            )
            
            print(f"\n✅ RESULT:")
            print(f"   Success: {result.get('success')}")
            print(f"   Message: {result.get('message')}")
            print(f"   Conversation ID: {result.get('conversation_id')}")
            
        except Exception as e:
            print(f"\n❌ ERROR: {e}")
            traceback.print_exc()
    
    print(f"\n{'='*80}")
    print("TEST COMPLETE")
    print(f"{'='*80}\n")


def test_tool_registration():
    """Test that tools can be registered properly"""
    
    print("=" * 80)
    print("TOOL REGISTRATION TEST")
    print("=" * 80)
    
    service = CommandExecutionService()
    
    # Get available commands
    commands = service.command_discovery.get_all_commands()
    
    print(f"\n📋 Available commands: {len(commands)}")
    for name, cmd in commands.items():
        print(f"   - {name}: {cmd.description}")
    
    # Test registration
    test_conversation_id = str(uuid.uuid4())
    
    print(f"\n🔧 Testing tool registration for conversation: {test_conversation_id}")
    success = service.register_tools_for_conversation(test_conversation_id)
    
    if success:
        print("✅ Tool registration successful!")
    else:
        print("❌ Tool registration failed!")
    
    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    # Check if user wants to run a specific test
    if len(sys.argv) > 1:
        test_type = sys.argv[1]
        
        if test_type == "registration":
            test_tool_registration()
        elif test_type == "calling":
            test_tool_calling()
        else:
            print(f"Unknown test type: {test_type}")
            print("Available tests: registration, calling")
    else:
        # Run all tests
        print("\n🧪 Running all tests...\n")
        test_tool_registration()
        print("\n")
        test_tool_calling()

