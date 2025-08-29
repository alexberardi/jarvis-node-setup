from typing import List, Any, Optional

from pydantic import BaseModel
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand
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
        return "Tells a joke, family-friendly or inappropriate, optionally on a specific topic"

    def generate_examples(self, date_context: DateContext) -> str:
        """Generate example utterances and how they get parsed into parameters using date context"""
        return f"""
        IMPORTANT: When voice commands mention relative dates like "tomorrow", "next week", etc., 
        you must parse them into actual datetime values and include them in the datetimes array.
        
        CRITICAL: All datetime values MUST include the full ISO format with time (YYYY-MM-DDTHH:MM:SSZ).
        Never return just the date (YYYY-MM-DD) - always include the time component.

        Voice Command: "Tell me a joke"
        → Output:
        {{"s":true,"n":"tell_a_joke_command","p":{{}},"e":null}}

        Voice Command: "Tell me a funny story"
        → Output:
        {{"s":true,"n":"tell_a_joke_command","p":{{"type":"story"}},"e":null}}

        Voice Command: "Make me laugh"
        → Output:
        {{"s":true,"n":"tell_a_joke_command","p":{{}},"e":null}}

        Voice Command: "Tell me a joke about today"
        → Output:
        {{"s":true,"n":"tell_a_joke_command","p":{{"context":"today","datetimes":["{date_context.current.utc_start_of_day}"]}},"e":null}}
        """
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("topic", "string", required=False, default=None, description="[OPTIONALLY REQUIRED]:The subject or topic for the joke (e.g., 'programming', 'animals', 'food'). If a subject is provided, this is required"),
            JarvisParameter("inappropriate", "bool", required=False, default=False, description="Whether to tell an inappropriate/adult joke (default: false)"),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required

    def run(self, request_info, **kwargs) -> CommandResponse:
        topic = kwargs.get("topic")
        inappropriate = kwargs.get("inappropriate", False)
        
        # Create the prompt for the LLM
        if inappropriate:
            if topic:
                prompt = f"Tell me an inappropriate joke about {topic}. You must respond with ONLY a JSON object like this: {{\"setup\": \"joke setup here\", \"punchline\": \"punchline here\"}}. This can be adult humor. RULE: Return ONLY the JSON object. Nothing else. No text before, no text after, no explanations, no markdown, no code blocks, no introductions, no conclusions. Just the JSON object."
            else:
                prompt = "Tell me an inappropriate joke. You must respond with ONLY a JSON object like this: {\"setup\": \"joke setup here\", \"punchline\": \"punchline here\"}. This can be adult humor. RULE: Return ONLY the JSON object. Nothing else. No text before, no text after, no explanations, no markdown, no code blocks, no introductions, no conclusions. Just the JSON object."
        else:
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
                    "requested_topic": kwargs.get("topic"),
                    "inappropriate": inappropriate
                }
            )
        else:
            # Fallback to a simple joke if LLM fails
            if inappropriate:
                fallback_joke = "Why did the scarecrow win an award? Because he was outstanding in his field!"
            else:
                fallback_joke = "Why don't scientists trust atoms? Because they make up everything!"
            
            return CommandResponse.final_response(
                speak_message=fallback_joke,
                context_data={
                    "setup": "Why don't scientists trust atoms?" if not inappropriate else "Why did the scarecrow win an award?",
                    "punchline": "Because they make up everything!" if not inappropriate else "Because he was outstanding in his field!",
                    "topic": "fallback",
                    "requested_topic": kwargs.get("topic"),
                    "inappropriate": inappropriate
                }
            )


class JokeResponse(BaseModel):
    setup: str
    punchline: str
