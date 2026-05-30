import re
from typing import Any, List

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
)
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


# Strong, unambiguous knowledge-question openers. These are deliberately
# narrow — pre-route iteration order across commands is non-deterministic
# (dict-of-commands iteration), so anything that could legitimately route to
# another command (time, weather, devices, measurement conversion, calendar)
# stays out of this list. "What is X" / "how many X in Y" / "where is X" are
# all excluded for that reason — they fall through to the LLM. Phrasings here
# unambiguously want a knowledge answer from the model's training data.
_KNOWLEDGE_PREFIX_RE = re.compile(
    r"^\s*("
    r"tell\s+me\s+about\s+\S+"      # "tell me about Einstein"
    r"|explain\s+\S+"                # "explain photosynthesis"
    r"|define\s+\S+"                 # "define entropy"
    r"|meaning\s+of\s+\S+"           # "meaning of ephemeral"
    r"|what(?:'?s|\s+is|\s+does)?\s+\S+\s+stand\s+for"  # "what does DNA stand for"
    r"|who\s+(?:is|was|were|are)\s+\S+"   # "who was Einstein"
    r"|when\s+(?:was|did)\s+\S+"          # "when was the constitution signed"
    r")\b",
    re.IGNORECASE,
)


class AnswerQuestionCommand(IJarvisCommand):
    """Answer general knowledge questions about stable facts."""

    @property
    def command_name(self) -> str:
        return "answer_question"

    @property
    def keywords(self) -> List[str]:
        return [
            "what is", "what are", "who is", "who was", "how far",
            "how many", "how much", "how does", "how do", "how old",
            "when was", "when did", "where is", "where was",
            "why is", "why does", "why do", "define", "explain",
            "tell me about", "what does", "meaning of",
        ]

    @property
    def description(self) -> str:
        return (
            "Answer a general knowledge question about stable facts, definitions, "
            "history, science, geography, math, or biographies. NOT for current events, "
            "live scores, or time-sensitive information."
        )

    @property
    def allow_direct_answer(self) -> bool:
        return True

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "query", "string", required=True,
                description="The knowledge question to answer.",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="search_web",
                description="Current events, live scores, trending topics, or time-sensitive information.",
            ),
            CommandAntipattern(
                command_name="get_sports",
                description="Sports scores, schedules, or game results for a specific team.",
            ),
            CommandAntipattern(
                command_name="get_weather",
                description="Weather forecasts or current conditions.",
            ),
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="How far is California from New Jersey?",
                expected_parameters={"query": "How far is California from New Jersey?"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="What is the capital of France?",
                expected_parameters={"query": "What is the capital of France?"},
            ),
            CommandExample(
                voice_command="Who invented the telephone?",
                expected_parameters={"query": "Who invented the telephone?"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return [
            # Geography / distance
            CommandExample(voice_command="How far is California from New Jersey?", expected_parameters={"query": "How far is California from New Jersey?"}, is_primary=True),
            CommandExample(voice_command="What's the tallest mountain in the world?", expected_parameters={"query": "What's the tallest mountain in the world?"}),
            CommandExample(voice_command="Where is the Great Wall of China?", expected_parameters={"query": "Where is the Great Wall of China?"}),

            # History / people
            CommandExample(voice_command="Who was the first president?", expected_parameters={"query": "Who was the first president?"}),
            CommandExample(voice_command="When was the Declaration of Independence signed?", expected_parameters={"query": "When was the Declaration of Independence signed?"}),
            CommandExample(voice_command="Who invented the light bulb?", expected_parameters={"query": "Who invented the light bulb?"}),

            # Science / definitions
            CommandExample(voice_command="What is photosynthesis?", expected_parameters={"query": "What is photosynthesis?"}),
            CommandExample(voice_command="How does gravity work?", expected_parameters={"query": "How does gravity work?"}),
            CommandExample(voice_command="What's the speed of light?", expected_parameters={"query": "What's the speed of light?"}),

            # Math / conversions
            CommandExample(voice_command="What is the square root of 144?", expected_parameters={"query": "What is the square root of 144?"}),
            CommandExample(voice_command="How many feet in a mile?", expected_parameters={"query": "How many feet in a mile?"}),

            # General knowledge
            CommandExample(voice_command="What does DNA stand for?", expected_parameters={"query": "What does DNA stand for?"}),
            CommandExample(voice_command="Tell me about Albert Einstein", expected_parameters={"query": "Tell me about Albert Einstein"}),
            CommandExample(voice_command="Explain the theory of relativity", expected_parameters={"query": "Explain the theory of relativity"}),
        ]

    # ------------------------------------------------------------------
    # Fast-path patterns — unambiguous knowledge prefixes bypass the LLM.
    # Pre-route iteration across commands is non-deterministic, so this
    # only fires on prefixes that can't legitimately route elsewhere
    # (no "what is", no "how many", no "where is").
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="answer_question.knowledge_prefix",
                description="Bypass LLM for 'tell me about X', 'explain X', 'define X', 'who is X', 'when was X', etc.",
                example="tell me about Albert Einstein",
                regex=_KNOWLEDGE_PREFIX_RE.pattern,
                handler="_fp_knowledge",
            ),
        ]

    def _fp_knowledge(self, match, voice_command: str) -> PreRouteResult | None:
        # Pass the full voice command through as the query — run() forwards
        # it unchanged so CC's downstream LLM call can answer the question
        # in full context.
        return PreRouteResult(arguments={"query": voice_command.strip()})

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        query: str = kwargs.get("query", request_info.voice_command)

        return CommandResponse.follow_up_response(
            context_data={
                "voice_command": request_info.voice_command,
                "query": query,
            },
        )
