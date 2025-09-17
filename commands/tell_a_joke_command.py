from typing import List

from pydantic import BaseModel
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from clients.jarvis_command_center_client import JarvisCommandCenterClient
from utils.config_service import Config


class TellAJokeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "tell_a_joke"

    @property
    def keywords(self) -> List[str]:
        return ["joke", "funny", "humor", "laugh"]

    @property
    def description(self) -> str:
        return "Tells a family-friendly joke, optionally on a specific topic"

    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate example utterances with expected parameters using date context"""
        return [
            CommandExample(
                voice_command="Tell me a joke",
                expected_parameters={},
                is_primary=True  # Primary example for command inference
            ),
            CommandExample(
                voice_command="Tell me a funny story",
                expected_parameters={"topic": "story"}
            ),
            CommandExample(
                voice_command="Make me laugh",
                expected_parameters={}
            ),
            CommandExample(
                voice_command="Tell me a joke about today",
                expected_parameters={"topic": "today"}
            )
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("topic", "string", required=False, default=None, description="[OPTIONALLY REQUIRED]:The subject or topic for the joke (e.g., 'programming', 'animals', 'food'). If a subject is provided, this is required"),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required

    def run(self, request_info, **kwargs) -> CommandResponse:
        topic = kwargs.get("topic")
        
        # Create the prompt for the LLM
        if topic:
            prompt = f"Tell me a joke about {topic}. You must respond with ONLY a JSON object like this: {{\"setup\": \"joke setup here\", \"punchline\": \"punchline here\"}}. Keep it clean and family-friendly. RULE: Return ONLY the JSON object. Nothing else. No text before, no text after, no explanations, no markdown, no code blocks, no introductions, no conclusions. Just the JSON object."
        else:
            prompt = "Tell me a joke. You must respond with ONLY a JSON object like this: {\"setup\": \"joke setup here\", \"punchline\": \"punchline here\"}. Keep it clean and family-friendly. RULE: Return ONLY the JSON object. Nothing else. No text before, no text after, no explanations, no markdown, no code blocks, no introductions, no conclusions. Just the JSON object."
        
        # Get joke from LLM
        jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
        joke_response = jcc_client.chat(prompt, JokeResponse)
        
        if joke_response and joke_response.setup and joke_response.punchline:
            setup = joke_response.setup
            punchline = joke_response.punchline
            return CommandResponse.final_response(
                speak_message=f"{setup}... {punchline}",
                context_data={
                    "setup": setup,
                    "punchline": punchline,
                    "topic": topic if topic else "random",
                    "requested_topic": kwargs.get("topic")
                }
            )
        else:
            # Fallback to a simple joke if LLM fails
            fallback_joke = "Why don't scientists trust atoms? Because they make up everything!"
            
            return CommandResponse.final_response(
                speak_message=fallback_joke,
                context_data={
                    "setup": "Why don't scientists trust atoms?",
                    "punchline": "Because they make up everything!",
                    "topic": "fallback",
                    "requested_topic": kwargs.get("topic")
                }
            )


class JokeResponse(BaseModel):
    setup: str
    punchline: str
