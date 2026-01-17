from typing import List, Any, Optional

from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse


class GeneralKnowledgeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "answer_question"

    @property
    def keywords(self) -> List[str]:
        return ["knowledge", "query", "what is", "who was", "when did", "where is", "how does", "explain", "history", "geography", "science", "facts", "definition", "meaning"]

    @property
    def description(self) -> str:
        return "Answer stable facts and definitions. Use for history, science, geography, and people. Not for current events, weather, sports, personal info, or calculations."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate example utterances with expected parameters using date context"""
        return [
            CommandExample(
                voice_command="What is the capital of France?",
                expected_parameters={"query": "What is the capital of France?"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Who was Albert Einstein?",
                expected_parameters={"query": "Who was Albert Einstein?"}
            ),
            CommandExample(
                voice_command="How does photosynthesis work?",
                expected_parameters={"query": "How does photosynthesis work?"}
            ),
            CommandExample(
                voice_command="Where is Mount Everest located?",
                expected_parameters={"query": "Where is Mount Everest located?"}
            ),
            CommandExample(
                voice_command="What is the definition of democracy?",
                expected_parameters={"query": "What is the definition of democracy?"}
            )
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("query", "string", required=True, description="Question about established knowledge."),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command for ESTABLISHED facts, historical information, scientific concepts, and timeless knowledge",
            "Do NOT use this for current events, recent news, live data, or information that changes frequently",
            "Do NOT use a live lookup for non-time-sensitive facts (e.g., locations, definitions, biographies)",
            "If the request is about 'latest', 'current', 'recent', or other time-sensitive info, do not use this command"
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="search_web",
                description="Current events or live information."
            )
        ]

    def run(self, request_info, **kwargs) -> CommandResponse:
        query = kwargs.get("query")
        
        if not query:
            return CommandResponse.error_response(
                error_details="Question parameter is required",
                context_data={
                    "query": None,
                    "error": "Query parameter is required"
                }
            )
        
        # Return raw query - server will answer it
        return CommandResponse.success_response(
            context_data={
                "query": query
            }
        )
