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
        return ["joke", "funny", "humor", "laugh"]

    @property
    def description(self) -> str:
        return "Tell a clean, family-friendly joke; optional topic. If user says 'about X', set topic. Not for stories, riddles, or live humor."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate example utterances with expected parameters using date context"""
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
