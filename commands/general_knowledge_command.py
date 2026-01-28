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

        Optimized for 3B model:
        - Always show query parameter with the full question
        - Heavy repetition of question patterns
        - Distinguish from search_web (stable facts vs current events)
        """
        examples = [
            # === CRITICAL: "What is" factual questions ===
            CommandExample(voice_command="What is the capital of France?", expected_parameters={"query": "What is the capital of France?"}, is_primary=True),
            CommandExample(voice_command="What is the capital of Germany?", expected_parameters={"query": "What is the capital of Germany?"}, is_primary=False),
            CommandExample(voice_command="What is the capital of Japan?", expected_parameters={"query": "What is the capital of Japan?"}, is_primary=False),
            CommandExample(voice_command="What is the capital of Canada?", expected_parameters={"query": "What is the capital of Canada?"}, is_primary=False),
            CommandExample(voice_command="What is the capital of Italy?", expected_parameters={"query": "What is the capital of Italy?"}, is_primary=False),
            CommandExample(voice_command="What is an atom?", expected_parameters={"query": "What is an atom?"}, is_primary=False),
            CommandExample(voice_command="What is gravity?", expected_parameters={"query": "What is gravity?"}, is_primary=False),
            CommandExample(voice_command="What is photosynthesis?", expected_parameters={"query": "What is photosynthesis?"}, is_primary=False),
            CommandExample(voice_command="What is DNA?", expected_parameters={"query": "What is DNA?"}, is_primary=False),
            CommandExample(voice_command="What is the speed of light?", expected_parameters={"query": "What is the speed of light?"}, is_primary=False),
            CommandExample(voice_command="What is the boiling point of water?", expected_parameters={"query": "What is the boiling point of water?"}, is_primary=False),

            # === "Who was/is" person questions ===
            CommandExample(voice_command="Who was Albert Einstein?", expected_parameters={"query": "Who was Albert Einstein?"}, is_primary=False),
            CommandExample(voice_command="Who was Marie Curie?", expected_parameters={"query": "Who was Marie Curie?"}, is_primary=False),
            CommandExample(voice_command="Who was Abraham Lincoln?", expected_parameters={"query": "Who was Abraham Lincoln?"}, is_primary=False),
            CommandExample(voice_command="Who was Cleopatra?", expected_parameters={"query": "Who was Cleopatra?"}, is_primary=False),
            CommandExample(voice_command="Who is Isaac Newton?", expected_parameters={"query": "Who is Isaac Newton?"}, is_primary=False),
            CommandExample(voice_command="Who invented the telephone?", expected_parameters={"query": "Who invented the telephone?"}, is_primary=False),
            CommandExample(voice_command="Who wrote Romeo and Juliet?", expected_parameters={"query": "Who wrote Romeo and Juliet?"}, is_primary=False),
            CommandExample(voice_command="Who discovered penicillin?", expected_parameters={"query": "Who discovered penicillin?"}, is_primary=False),

            # === "Where is" location questions ===
            CommandExample(voice_command="Where is Mount Everest?", expected_parameters={"query": "Where is Mount Everest?"}, is_primary=False),
            CommandExample(voice_command="Where is the Eiffel Tower?", expected_parameters={"query": "Where is the Eiffel Tower?"}, is_primary=False),
            CommandExample(voice_command="Where is the Amazon River?", expected_parameters={"query": "Where is the Amazon River?"}, is_primary=False),
            CommandExample(voice_command="Where is Antarctica?", expected_parameters={"query": "Where is Antarctica?"}, is_primary=False),
            CommandExample(voice_command="Where is the Sahara Desert?", expected_parameters={"query": "Where is the Sahara Desert?"}, is_primary=False),

            # === "When did" historical questions ===
            CommandExample(voice_command="When did World War II end?", expected_parameters={"query": "When did World War II end?"}, is_primary=False),
            CommandExample(voice_command="When did World War I start?", expected_parameters={"query": "When did World War I start?"}, is_primary=False),
            CommandExample(voice_command="When was the moon landing?", expected_parameters={"query": "When was the moon landing?"}, is_primary=False),
            CommandExample(voice_command="When did the dinosaurs go extinct?", expected_parameters={"query": "When did the dinosaurs go extinct?"}, is_primary=False),
            CommandExample(voice_command="When was the Declaration of Independence signed?", expected_parameters={"query": "When was the Declaration of Independence signed?"}, is_primary=False),

            # === "How does/do" explanation questions ===
            CommandExample(voice_command="How does photosynthesis work?", expected_parameters={"query": "How does photosynthesis work?"}, is_primary=False),
            CommandExample(voice_command="How does gravity work?", expected_parameters={"query": "How does gravity work?"}, is_primary=False),
            CommandExample(voice_command="How do airplanes fly?", expected_parameters={"query": "How do airplanes fly?"}, is_primary=False),
            CommandExample(voice_command="How do batteries work?", expected_parameters={"query": "How do batteries work?"}, is_primary=False),
            CommandExample(voice_command="How does the heart pump blood?", expected_parameters={"query": "How does the heart pump blood?"}, is_primary=False),

            # === "Explain" questions ===
            CommandExample(voice_command="Explain photosynthesis", expected_parameters={"query": "Explain photosynthesis"}, is_primary=False),
            CommandExample(voice_command="Explain gravity", expected_parameters={"query": "Explain gravity"}, is_primary=False),
            CommandExample(voice_command="Explain the water cycle", expected_parameters={"query": "Explain the water cycle"}, is_primary=False),
            CommandExample(voice_command="Explain quantum physics", expected_parameters={"query": "Explain quantum physics"}, is_primary=False),
            CommandExample(voice_command="Explain how computers work", expected_parameters={"query": "Explain how computers work"}, is_primary=False),

            # === "Define" / definition questions ===
            CommandExample(voice_command="Define photosynthesis", expected_parameters={"query": "Define photosynthesis"}, is_primary=False),
            CommandExample(voice_command="Define democracy", expected_parameters={"query": "Define democracy"}, is_primary=False),
            CommandExample(voice_command="What is the definition of entropy?", expected_parameters={"query": "What is the definition of entropy?"}, is_primary=False),
            CommandExample(voice_command="What does entropy mean?", expected_parameters={"query": "What does entropy mean?"}, is_primary=False),

            # === Comparative/superlative facts ===
            CommandExample(voice_command="What is the largest planet?", expected_parameters={"query": "What is the largest planet?"}, is_primary=False),
            CommandExample(voice_command="What is the longest river in the world?", expected_parameters={"query": "What is the longest river in the world?"}, is_primary=False),
            CommandExample(voice_command="What is the tallest mountain?", expected_parameters={"query": "What is the tallest mountain?"}, is_primary=False),
            CommandExample(voice_command="How many continents are there?", expected_parameters={"query": "How many continents are there?"}, is_primary=False),
            CommandExample(voice_command="How many bones in the human body?", expected_parameters={"query": "How many bones in the human body?"}, is_primary=False),

            # === Casual/short questions ===
            CommandExample(voice_command="Capital of Japan?", expected_parameters={"query": "Capital of Japan?"}, is_primary=False),
            CommandExample(voice_command="Capital of Spain?", expected_parameters={"query": "Capital of Spain?"}, is_primary=False),
            CommandExample(voice_command="What's a black hole?", expected_parameters={"query": "What's a black hole?"}, is_primary=False),
            CommandExample(voice_command="What's DNA?", expected_parameters={"query": "What's DNA?"}, is_primary=False),
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
