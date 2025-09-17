import re
from typing import List

from pydantic import BaseModel
from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from services.chunked_command_response_service import ChunkedCommandResponseService
from services.secret_service import get_secret_value_int
from utils.config_service import Config

# --- Pydantic model for LLM JSON response ---
class StoryChunk(BaseModel):
    message: str


class StoryCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "tell_a_story"
    
    @property
    def description(self) -> str:
        return "Writes and reads a story to the user in chunks. Use 'continue' to hear more, 'end story' to finish."

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("word_count", "int", required=False, default="750", description="Target word count for the story"),
            JarvisParameter("story_subject", "string", required=False, description="Subject or theme for the story"),
            JarvisParameter("target_audience_age", "int", required=False, default="5", description="Target age for the story content"),
            JarvisParameter("session_id", "string", required=False, description="Session ID to continue an existing story"),
            JarvisParameter("action", "string", required=False, default="start", description="Action to perform: 'start', 'continue', or 'end'")
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("JARVIS_STORY_TARGET_AUDIENCE_AGE_DEFAULT", "Target audience age for the story", "integration", "int")
        ]

    @property
    def keywords(self) -> List[str]:
        return ["story", "tell me a story", "continue story", "end story"]

    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate examples for the story command"""
        return [
            CommandExample(
                voice_command="Tell me a story",
                expected_parameters={},
                is_primary=True
            ),
            CommandExample(
                voice_command="Tell me a story about dragons",
                expected_parameters={"story_subject": "dragons"}
            ),
            CommandExample(
                voice_command="Tell me a 500 word story",
                expected_parameters={"word_count": 500}
            ),
            CommandExample(
                voice_command="Continue story",
                expected_parameters={"action": "continue"}
            ),
            CommandExample(
                voice_command="End story",
                expected_parameters={"action": "end"}
            )
        ]

    def run(self, request_info, **kwargs) -> CommandResponse:
        # ---- inputs & defaults ----
        action = kwargs.get("action", "start")
        session_id = kwargs.get("session_id")
        
        # Initialize the chunked command response service
        chunked_service = ChunkedCommandResponseService()
        
        if action == "continue" and session_id:
            # Continue an existing story
            return self._continue_story(chunked_service, session_id, request_info)
        elif action == "end" and session_id:
            # End an existing story
            return self._end_story(chunked_service, session_id, request_info)
        else:
            # Start a new story
            return self._start_story(chunked_service, request_info, **kwargs)
    
    def _start_story(self, chunked_service: ChunkedCommandResponseService, request_info, **kwargs) -> CommandResponse:
        """Start a new story session."""
        # ---- inputs & defaults ----
        raw_wc = kwargs.get("word_count")
        word_count: int
        if isinstance(raw_wc, int):
            word_count = raw_wc
        else:
            try:
                word_count = int(raw_wc) if raw_wc is not None else 750
            except Exception:
                word_count = 750

        # subject
        story_subject = kwargs.get("story_subject")
        if not story_subject or not str(story_subject).strip():
            story_subject = "about anything"
        else:
            story_subject = str(story_subject).strip()

        # target audience age
        age = kwargs.get("target_audience_age")
        if age is None:
            try:
                age = get_secret_value_int("JARVIS_STORY_TARGET_AUDIENCE_AGE_DEFAULT", "integration")
            except Exception:
                age = None
        try:
            target_age = int(age) if age is not None else 5
        except Exception:
            target_age = 5

        # ---- LLM client ----
        base_url = Config.get_str("jarvis_llm_proxy_api_url", "http://localhost:8000")
        client = JarvisCommandCenterClient(base_url)

        # ---- chunking strategy ----
        TARGET_CHUNK_WORDS = 400
        REMAINING = max(150, int(word_count))  # guard against tiny values


        # helper to trim to word boundary & end of sentence
        END_SENTENCE_RE = re.compile(r"([.!?][\"\']?\s+)")

        def trim_to_words(text: str, max_words: int) -> str:
            words = text.split()
            if len(words) <= max_words:
                return text.strip()
            partial = " ".join(words[: max_words + 50])  # cushion
            chunks = END_SENTENCE_RE.split(partial)
            out = ""
            for i in range(0, len(chunks) - 1, 2):
                candidate = out + chunks[i] + chunks[i + 1]
                if len(candidate.split()) > max_words:
                    break
                out = candidate
            return (out.strip() or " ".join(words[:max_words]).strip())

        # ---- Start the chunked session ----
        session_id = chunked_service.start_session("story_command")
        
        # ---- Generate first chunk ----
        chunk_budget = min(TARGET_CHUNK_WORDS, REMAINING)
        is_final = REMAINING <= TARGET_CHUNK_WORDS

        # system-style guidance kept compact for the lightweight model
        prompt = (
            "You are Jarvis, a gentle bedtime storyteller.\n\n"
            "TASK: Write a calm, age-appropriate story segment.\n"
            "Return VALID JSON only: {\"message\": \"<story text>\"}.\n"
            "Do not add any other keys or commentary.\n\n"
            f"GOAL: Tell me a {word_count} word story {story_subject} for a {target_age} year old.\n"
            "Write the next ~{chunk_budget} words.\n"
            "Continue seamlessly after this previous ending (may be empty):\n"
            "\"\"\n\n"
            "CONSTRAINTS:\n"
            "- Soft, cozy tone; simple sentences; no scary content.\n"
            "- End on a complete sentence.\n"
            "- Do NOT write 'The end'" + (" until I say final." if not is_final else " yet; close gently now with 'The end.'") + "\n"
        )

        # Ask the lightweight LLM to return JSON {"message": "..."}
        result = client.lightweight_chat(prompt, StoryChunk)
        if not result or not isinstance(result, StoryChunk):
            # Clean up the session if we failed
            chunked_service.end_session(session_id)
            return CommandResponse.error_response(
                speak_message="Sorry, I couldn't start the story just now. Please try again.",
                error_details="LLM failed to generate initial story chunk"
            )

        raw_text = (result.message or "").strip()
        if not raw_text:
            # Clean up the session if we got empty content
            chunked_service.end_session(session_id)
            return CommandResponse.error_response(
                speak_message="Sorry, the story chunk was empty. Please try again.",
                error_details="LLM returned empty story content"
            )

        # Trim and add to session
        trimmed = trim_to_words(raw_text, chunk_budget)
        words_emitted = len(trimmed.split())
        REMAINING -= words_emitted

        # Add the first chunk to the session
        chunked_service.append_content(session_id, trimmed)
        
        # Return response with session info
        return CommandResponse.chunked_response(
            speak_message=f"I've started a story about {story_subject} for a {target_age} year old. I'll tell it in chunks. Say 'continue' to hear more, or 'end story' when you're done.",
            session_id=session_id,
            context_data={
                "session_id": session_id,
                "subject": story_subject,
                "target_age": target_age,
                "requested_words": word_count,
                "generated_words": words_emitted,
                "remaining_words": REMAINING,
                "chunks_generated": 1,
                "action": "start",
                "is_chunked": True
            }
        )
    
    def _continue_story(self, chunked_service: ChunkedCommandResponseService, session_id: str, request_info) -> CommandResponse:
        """Continue an existing story by generating the next chunk."""
        # Get the current session status
        status = chunked_service.get_session_status(session_id)
        if not status:
            return CommandResponse.error_response(
                speak_message="I couldn't find that story session. Let me start a new one for you.",
                error_details=f"Session {session_id} not found"
            )
        
        # Get the LLM client
        base_url = Config.get_str("jarvis_llm_proxy_api_url", "http://localhost:8000")
        client = JarvisCommandCenterClient(base_url)
        
        # Get the current content to find the last few words for continuity
        db_record = chunked_service.repository.get_by_session_id(session_id)
        if not db_record:
            return CommandResponse.error_response(
                speak_message="I couldn't retrieve the story. Let me start a new one for you.",
                error_details=f"Database record for session {session_id} not found"
            )
        
        # Get the last few words for continuity
        last_words = db_record.full_content[-100:] if len(db_record.full_content) > 100 else db_record.full_content
        
        # Generate the next chunk
        prompt = (
            "You are Jarvis, a gentle bedtime storyteller.\n\n"
            "TASK: Continue the story seamlessly from where it left off.\n"
            "Return VALID JSON only: {\"message\": \"<story text>\"}.\n"
            "Do not add any other keys or commentary.\n\n"
            "STORY SO FAR (last few words):\n"
            f"\"{last_words}\"\n\n"
            "CONSTRAINTS:\n"
            "- Continue naturally from the previous text\n"
            "- Soft, cozy tone; simple sentences; no scary content\n"
            "- End on a complete sentence\n"
            "- Write about 200-400 words\n"
            "- Do NOT write 'The end' yet\n"
        )
        
        result = client.lightweight_chat(prompt, StoryChunk)
        if not result or not isinstance(result, StoryChunk):
            return CommandResponse.error_response(
                speak_message="Sorry, I couldn't continue the story just now. Please try again.",
                error_details="LLM failed to generate story continuation"
            )
        
        raw_text = (result.message or "").strip()
        if not raw_text:
            return CommandResponse.error_response(
                speak_message="Sorry, the story continuation was empty. Please try again.",
                error_details="LLM returned empty story continuation"
            )
        
        # Add the new chunk to the session
        chunked_service.append_content(session_id, " " + raw_text)
        
        return CommandResponse.chunked_response(
            speak_message=f"I've added more to your story. Say 'continue' to hear more, or 'end story' when you're ready to finish.",
            session_id=session_id,
            context_data={
                "session_id": session_id,
                "action": "continue",
                "is_chunked": True,
                "new_content_added": True
            }
        )
    
    def _end_story(self, chunked_service: ChunkedCommandResponseService, session_id: str, request_info) -> CommandResponse:
        """End an existing story session."""
        # Get the current session status
        status = chunked_service.get_session_status(session_id)
        if not status:
            return CommandResponse.error_response(
                speak_message="I couldn't find that story session.",
                error_details=f"Session {session_id} not found"
            )
        
        # Get the final content
        db_record = chunked_service.repository.get_by_session_id(session_id)
        if not db_record:
            return CommandResponse.error_response(
                speak_message="I couldn't retrieve the story content.",
                error_details=f"Database record for session {session_id} not found"
            )
        
        # Add a proper ending
        ending = "\n\nThe end."
        chunked_service.append_content(session_id, ending)
        
        # End the session
        chunked_service.end_session(session_id)
        
        # Get final stats
        final_content = db_record.full_content + ending
        word_count = len(final_content.split())
        
        return CommandResponse.final_response(
            speak_message=f"Your story is now complete! It was {word_count} words long. I hope you enjoyed it!",
            context_data={
                "session_id": session_id,
                "action": "end",
                "is_chunked": False,
                "story_completed": True,
                "final_word_count": word_count
            }
        )
