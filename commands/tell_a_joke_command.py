from typing import List

from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse


class TellAJokeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "tell_joke"

    @property
    def keywords(self) -> List[str]:
        return ["joke", "funny", "humor", "laugh", "comedy", "make me laugh"]

    @property
    def description(self) -> str:
        return "Tell a clean, family-friendly joke with an optional topic."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date context"""
        return [
            CommandExample(
                voice_command="Tell me a joke",
                expected_parameters={},
                is_primary=True  # Primary example for command inference
            ),
            CommandExample(
                voice_command="Tell me a joke about animals",
                expected_parameters={"topic": "animals"}
            ),
            CommandExample(
                voice_command="Make me laugh with a joke about technology",
                expected_parameters={"topic": "technology"}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Optimized for 3B model:
        - Fewer examples per pattern (1-2 per variation)
        - Clear "no topic = empty params" pattern
        - Topic extraction from "about X" phrases
        """
        examples: List[CommandExample] = [
            # === No topic ===
            CommandExample(voice_command="Tell me a joke", expected_parameters={}, is_primary=True),
            CommandExample(voice_command="Make me laugh", expected_parameters={}, is_primary=False),
            CommandExample(voice_command="Say something funny", expected_parameters={}, is_primary=False),

            # === Topic extraction ===
            CommandExample(voice_command="Tell me a joke about cats", expected_parameters={"topic": "cats"}, is_primary=False),
            CommandExample(voice_command="Tell me a joke about programming", expected_parameters={"topic": "programming"}, is_primary=False),
            CommandExample(voice_command="Tell me a joke about sports", expected_parameters={"topic": "sports"}, is_primary=False),

            # === Varied phrasing with topic ===
            CommandExample(voice_command="Make me laugh with a joke about technology", expected_parameters={"topic": "technology"}, is_primary=False),
            CommandExample(voice_command="Got any jokes about animals?", expected_parameters={"topic": "animals"}, is_primary=False),

            # === Humor style as topic ===
            CommandExample(voice_command="Tell me a dad joke", expected_parameters={"topic": "dad jokes"}, is_primary=False),
            CommandExample(voice_command="Give me a knock knock joke", expected_parameters={"topic": "knock knock"}, is_primary=False),
        ]
        return examples
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("topic", "string", required=False, default=None, description="Optional topic; omit for a random joke."),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required

    def run(self, request_info, **kwargs) -> CommandResponse:
        topic = kwargs.get("topic")
        
        # Return raw request - server will generate the joke
        return CommandResponse.success_response(
            context_data={
                "topic": topic if topic else "random"
            }
        )
