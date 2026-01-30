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
        return ["knowledge", "query", "what is", "who was", "who is", "when did", "where is", "how does", "define", "explain"]

    @property
    def description(self) -> str:
        return "Answer questions about stable facts, definitions, history, science, geography, and biographies. Use ONLY for established, non-changing knowledge."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date context"""
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

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Focus areas:
        - "Explain [topic]" - stable knowledge, not current events
        - "What is [concept]" - definitions and facts
        - "Who was/is [person]" - biographies
        - "How does [thing] work" - scientific explanations
        """
        examples = [
            # === "EXPLAIN" - Scientific/educational topics ===
            CommandExample(voice_command="Explain quantum physics", expected_parameters={"query": "Explain quantum physics"}, is_primary=True),
            CommandExample(voice_command="Explain gravity", expected_parameters={"query": "Explain gravity"}, is_primary=False),
            CommandExample(voice_command="Explain how the internet works", expected_parameters={"query": "Explain how the internet works"}, is_primary=False),
            CommandExample(voice_command="Explain evolution", expected_parameters={"query": "Explain evolution"}, is_primary=False),
            CommandExample(voice_command="Explain the theory of relativity", expected_parameters={"query": "Explain the theory of relativity"}, is_primary=False),

            # === "WHAT IS" factual ===
            CommandExample(voice_command="What is the capital of France?", expected_parameters={"query": "What is the capital of France?"}, is_primary=False),
            CommandExample(voice_command="What is photosynthesis?", expected_parameters={"query": "What is photosynthesis?"}, is_primary=False),
            CommandExample(voice_command="What is a black hole?", expected_parameters={"query": "What is a black hole?"}, is_primary=False),
            CommandExample(voice_command="What is the largest planet?", expected_parameters={"query": "What is the largest planet?"}, is_primary=False),

            # === "WHO" person questions ===
            CommandExample(voice_command="Who was Albert Einstein?", expected_parameters={"query": "Who was Albert Einstein?"}, is_primary=False),
            CommandExample(voice_command="Who invented the telephone?", expected_parameters={"query": "Who invented the telephone?"}, is_primary=False),
            CommandExample(voice_command="Who was Shakespeare?", expected_parameters={"query": "Who was Shakespeare?"}, is_primary=False),

            # === "WHERE IS" / "WHEN DID" ===
            CommandExample(voice_command="Where is Mount Everest?", expected_parameters={"query": "Where is Mount Everest?"}, is_primary=False),
            CommandExample(voice_command="Where is the Sahara Desert?", expected_parameters={"query": "Where is the Sahara Desert?"}, is_primary=False),
            CommandExample(voice_command="When did World War II end?", expected_parameters={"query": "When did World War II end?"}, is_primary=False),
            CommandExample(voice_command="When was the Declaration of Independence signed?", expected_parameters={"query": "When was the Declaration of Independence signed?"}, is_primary=False),

            # === "HOW DOES" / scientific ===
            CommandExample(voice_command="How does photosynthesis work?", expected_parameters={"query": "How does photosynthesis work?"}, is_primary=False),
            CommandExample(voice_command="How does a computer work?", expected_parameters={"query": "How does a computer work?"}, is_primary=False),

            # === "DEFINE" ===
            CommandExample(voice_command="Define democracy", expected_parameters={"query": "Define democracy"}, is_primary=False),
            CommandExample(voice_command="Define entropy", expected_parameters={"query": "Define entropy"}, is_primary=False),

            # === CASUAL / "TELL ME ABOUT" ===
            CommandExample(voice_command="Tell me about the solar system", expected_parameters={"query": "Tell me about the solar system"}, is_primary=False),
            CommandExample(voice_command="How many continents are there?", expected_parameters={"query": "How many continents are there?"}, is_primary=False),
        ]
        return examples
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("query", "string", required=True, description="Question about established, stable knowledge."),
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
                description="Current events, live information, election results, 'who won' recent races or championships, real-time data, breaking news. Time zones, current time in locations ('what time is it in X')."
            ),
            CommandAntipattern(
                command_name="convert_measurement",
                description="Unit conversions (miles to km, cups to liters, pounds to kg). Use convert_measurement for ALL unit conversions."
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
