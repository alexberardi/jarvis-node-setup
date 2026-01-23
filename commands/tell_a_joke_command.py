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
        """Generate varied examples for adapter training"""
        topics = [
            None, "animals", "technology", "programming", "sports", "food", "space",
            "music", "school", "science", "history", "travel", "cats", "dogs", "robots",
            "computers", "movies", "books", "pirates", "nature", "weather", "cars",
            "video games", "math", "office", "kids", "family", "work", "coffee", "pizza",
            "astronomy", "dinosaurs", "superheroes", "cooking", "gardening", "planes",
            "trains", "bicycles", "oceans", "mountains"
        ]
        phrases = [
            "Tell me a joke",
            "I need a joke",
            "Make me laugh",
            "Say something funny",
            "Give me a quick joke",
            "Tell a clean joke",
            "Can you tell a joke?",
            "I want a joke",
            "Share a joke",
            "Give me a funny joke"
        ]
        examples: List[CommandExample] = []
        is_primary = True
        for topic in topics:
            for phrase in phrases:
                if len(examples) >= 40:
                    break
                if topic:
                    voice = f"{phrase} about {topic}"
                    params = {"topic": topic}
                else:
                    voice = phrase
                    params = {}
                examples.append(CommandExample(voice_command=voice, expected_parameters=params, is_primary=is_primary))
                is_primary = False
            if len(examples) >= 40:
                break

        # Casual/varied phrasings (no explicit "joke" word)
        varied_examples = [
            ("Make me laugh", {}),
            ("Hit me with something funny", {}),
            ("Got any good ones?", {}),
            ("Cheer me up", {}),
        ]
        for voice, params in varied_examples:
            examples.append(CommandExample(voice_command=voice, expected_parameters=params, is_primary=False))

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
