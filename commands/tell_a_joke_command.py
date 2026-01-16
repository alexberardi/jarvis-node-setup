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
        return "Tell a clean, family-friendly joke, optionally on a requested topic. Use when asked for a joke or to make someone laugh. Do NOT use for stories, riddles/brain-teasers, or current-event humor that needs a live lookup."

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
            JarvisParameter("topic", "string", required=False, default=None, description="Optional topic or subject for the joke (e.g., 'programming', 'animals', 'food', 'science', 'knock-knock'). If provided, the joke will be related to this topic. If omitted, a random family-friendly joke will be told."),
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
