from typing import List, Any, Optional

from pydantic import BaseModel
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from clients.jarvis_command_center_client import JarvisCommandCenterClient
from utils.config_service import Config


class GeneralKnowledgeCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "general_knowledge_command"

    @property
    def keywords(self) -> List[str]:
        return ["knowledge", "query", "what is", "who was", "when did", "where is", "how does", "explain", "history", "geography", "science", "facts"]

    @property
    def description(self) -> str:
        return "Answers general knowledge questions using AI, covering topics like history, geography, science, and more"

    def generate_examples(self, date_context: DateContext) -> str:
        """Generate example utterances and how they get parsed into parameters using date context"""
        return f"""
        IMPORTANT: When voice commands mention relative dates like "tomorrow", "next week", etc., 
        you must parse them into actual datetime values and include them in the datetimes array.
        
        CRITICAL: All datetime values MUST include the full ISO format with time (YYYY-MM-DDTHH:MM:SSZ).
        Never return just the date (YYYY-MM-DD) - always include the time component.

        Voice Command: "What's the weather like?"
        → Output:
        {{"s":true,"n":"general_knowledge_command","p":{{"query":"What's the weather like?"}},"e":null}}

        Voice Command: "Tell me about artificial intelligence"
        → Output:
        {{"s":true,"n":"general_knowledge_command","p":{{"query":"Tell me about artificial intelligence"}},"e":null}}

        Voice Command: "What happened in history today?"
        → Output:
        {{"s":true,"n":"general_knowledge_command","p":{{"query":"What happened in history today?","datetimes":["{date_context.current.utc_start_of_day}"]}},"e":null}}

        Voice Command: "What events are happening tomorrow?"
        → Output:
        {{"s":true,"n":"general_knowledge_command","p":{{"query":"What events are happening tomorrow?","datetimes":["{date_context.relative_dates.tomorrow.utc_start_of_day}"]}},"e":null}}
        """
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("query", "string", required=True, description="The general knowledge query to be answered"),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No secrets required

    def run(self, request_info, **kwargs) -> CommandResponse:
        query = kwargs.get("query")
        
        if not query:
            return CommandResponse.error_response(
                speak_message="I'm sorry, but I didn't hear a question. Could you please ask me something?",
                error_details="Question parameter is required",
                context_data={
                    "query": None,
                    "answer": None,
                    "error": "Query parameter is required"
                }
            )
        
        # Create the prompt for the LLM
        prompt = f"Answer this general knowledge query: {query}\n\nYou must respond with ONLY a JSON object like this: {{\"answer\": \"your short, succinct answer here\"}}. Keep your answer informative, accurate, and concise - aim for 1-3 sentences maximum. RULE: Return ONLY the JSON object. Nothing else. No text before, no text after, no explanations, no markdown, no code blocks, no introductions, no conclusions. Just the JSON object."
        
        # Get answer from LLM
        jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
        knowledge_response = jcc_client.chat(prompt, KnowledgeResponse)
        
        if knowledge_response and knowledge_response.answer:
            return CommandResponse.follow_up_response(
                speak_message=knowledge_response.answer,
                context_data={
                    "query": query,
                    "answer": knowledge_response.answer
                }
            )
        else:
            # Fallback response if LLM fails
            fallback_answer = "I'm sorry, I couldn't generate an answer to that question at the moment. Please try rephrasing your question or ask something else."
            
            return CommandResponse.follow_up_response(
                speak_message=fallback_answer,
                context_data={
                    "query": query,
                    "answer": fallback_answer,
                    "note": "This is a fallback response as the LLM failed to generate an answer"
                }
            )


class KnowledgeResponse(BaseModel):
    answer: str
