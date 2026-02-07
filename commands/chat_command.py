"""Chat command for casual conversation with Jarvis.

Handles greetings, small talk, opinions, and open-ended conversation.
Returns follow_up_response to enable multi-turn conversation mode.
"""

from typing import List

from core.command_response import CommandResponse
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


class ChatCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "chat"

    @property
    def description(self) -> str:
        return "Have a casual conversation with Jarvis. Use for greetings, small talk, opinions, and open-ended chat."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    @property
    def keywords(self) -> List[str]:
        return [
            "chat", "talk", "hello", "hi", "hey",
            "how are you", "what's up", "good morning",
            "good evening", "good night", "good afternoon",
            "how's it going", "what do you think",
            "tell me about yourself", "who are you",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "message",
                "string",
                required=True,
                description="The user's conversational message or greeting"
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use ONLY for casual conversation, greetings, small talk, and opinions",
            "Do NOT use for factual questions - use answer_question instead",
            "Do NOT use for web lookups - use search_web instead",
            "Do NOT use for weather, timers, calculations, or device control",
            "Do NOT use for music playback - use play_music or control_music instead",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="answer_question",
                description="Factual questions, definitions, history, science, geography, biographies"
            ),
            CommandAntipattern(
                command_name="search_web",
                description="Current events, live information, news, recent data"
            ),
            CommandAntipattern(
                command_name="get_weather",
                description="Weather forecasts, temperature, conditions"
            ),
            CommandAntipattern(
                command_name="calculate",
                description="Math calculations, arithmetic, number crunching"
            ),
            CommandAntipattern(
                command_name="set_timer",
                description="Setting timers or alarms"
            ),
            CommandAntipattern(
                command_name="control_device",
                description="Smart home device control, turning on/off lights"
            ),
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Hey Jarvis, how's it going?",
                expected_parameters={"message": "Hey Jarvis, how's it going?"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Good morning!",
                expected_parameters={"message": "Good morning!"},
            ),
            CommandExample(
                voice_command="What do you think about space travel?",
                expected_parameters={"message": "What do you think about space travel?"},
            ),
            CommandExample(
                voice_command="Tell me something interesting",
                expected_parameters={"message": "Tell me something interesting"},
            ),
            CommandExample(
                voice_command="How are you doing today?",
                expected_parameters={"message": "How are you doing today?"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [
            # Greetings
            CommandExample(
                voice_command="Hey Jarvis, how's it going?",
                expected_parameters={"message": "Hey Jarvis, how's it going?"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Good morning!",
                expected_parameters={"message": "Good morning!"},
            ),
            CommandExample(
                voice_command="Hello there",
                expected_parameters={"message": "Hello there"},
            ),
            CommandExample(
                voice_command="Hi Jarvis",
                expected_parameters={"message": "Hi Jarvis"},
            ),
            CommandExample(
                voice_command="Good evening, how are you?",
                expected_parameters={"message": "Good evening, how are you?"},
            ),
            # Small talk
            CommandExample(
                voice_command="How are you doing today?",
                expected_parameters={"message": "How are you doing today?"},
            ),
            CommandExample(
                voice_command="What's up?",
                expected_parameters={"message": "What's up?"},
            ),
            CommandExample(
                voice_command="How's your day going?",
                expected_parameters={"message": "How's your day going?"},
            ),
            # Opinions / open-ended
            CommandExample(
                voice_command="What do you think about space travel?",
                expected_parameters={"message": "What do you think about space travel?"},
            ),
            CommandExample(
                voice_command="Tell me something interesting",
                expected_parameters={"message": "Tell me something interesting"},
            ),
            CommandExample(
                voice_command="Do you have a favorite color?",
                expected_parameters={"message": "Do you have a favorite color?"},
            ),
            CommandExample(
                voice_command="Tell me about yourself",
                expected_parameters={"message": "Tell me about yourself"},
            ),
            # Casual conversation
            CommandExample(
                voice_command="I'm having a great day",
                expected_parameters={"message": "I'm having a great day"},
            ),
            CommandExample(
                voice_command="Thanks Jarvis, you're awesome",
                expected_parameters={"message": "Thanks Jarvis, you're awesome"},
            ),
            CommandExample(
                voice_command="Let's just chat for a bit",
                expected_parameters={"message": "Let's just chat for a bit"},
            ),
        ]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        message: str = kwargs.get("message", "")

        if not message:
            return CommandResponse.error_response(
                error_details="Message parameter is required",
                context_data={"error": "Message parameter is required"},
            )

        return CommandResponse.follow_up_response(
            context_data={"message": message},
        )
