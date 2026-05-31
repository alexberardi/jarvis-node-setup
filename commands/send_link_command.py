"""SendLinkCommand — push a tap-to-open URL to the speaker's phone.

Designed as a follow-up to news, recipes, web search, etc. The LLM grabs a
URL from a prior tool result in conversation history (e.g. an article URL
returned by get_barstool / get_news) and calls this command with that URL.

Speaker-gated: if the speaker isn't identified, we refuse — a notification
without a target user would either get silently dropped or broadcast to the
whole household, neither of which is what the user asked for.
"""

from typing import Any, List

from jarvis_command_sdk import CommandExample, IJarvisCommand
from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation

logger = JarvisLogger(service="jarvis-node")


class SendLinkCommand(IJarvisCommand):
    """Send a link to the recognized speaker's phone as a tap-to-open push."""

    @property
    def command_name(self) -> str:
        return "send_link"

    @property
    def description(self) -> str:
        return (
            "Send a link (URL) to the speaker's phone as a push notification "
            "they can tap to open in their browser. Use as a follow-up when the "
            "user asks to be sent / texted / shared an article, recipe, search "
            "result, or any other link returned by a previous tool. Requires "
            "speaker recognition — fails if Jarvis can't identify who's asking."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "send me", "send that", "send the link", "send it",
            "text me", "push me", "share that",
            "send to my phone", "to my phone", "open on my phone",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "url",
                "string",
                required=True,
                description=(
                    "Full http/https URL to send. Pull this from a previous "
                    "tool result in the conversation history — do NOT invent "
                    "or guess URLs."
                ),
            ),
            JarvisParameter(
                "title",
                "string",
                required=False,
                description=(
                    "Short title shown in the push notification (e.g. article "
                    "headline). Defaults to 'Link from Jarvis'."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Send me that article",
                expected_parameters={
                    "url": "https://www.barstoolsports.com/blog/123/example",
                    "title": "Example Barstool article",
                },
                is_primary=True,
            ),
            CommandExample(
                voice_command="Send the second one to my phone",
                expected_parameters={
                    "url": "https://example.com/article",
                    "title": "Example article",
                },
            ),
            CommandExample(
                voice_command="Text me that link",
                expected_parameters={"url": "https://example.com/link"},
            ),
            CommandExample(
                voice_command="Open that on my phone",
                expected_parameters={"url": "https://example.com/recipe"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        sample_url = "https://example.com/article"
        phrases = [
            "Send me that article",
            "Send that to my phone",
            "Send me the link",
            "Send the first one to my phone",
            "Send the second one to my phone",
            "Text me that link",
            "Push that to my phone",
            "Share that link with me",
            "Open that on my phone",
            "Send me that recipe",
            "Send me the story",
        ]
        return [
            CommandExample(voice_command=p, expected_parameters={"url": sample_url})
            for p in phrases
        ]

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        speaker_user_id = request_info.user_id
        url = (kwargs.get("url") or "").strip()
        title_param = (kwargs.get("title") or "").strip()
        logger.info(
            "send_link invoked",
            user_id=speaker_user_id,
            url=url[:120],
            title=title_param[:80],
        )

        if speaker_user_id is None:
            logger.warning("send_link refused: no speaker_user_id")
            return CommandResponse.error_response(
                error_details=(
                    "I couldn't tell who's asking, so I can't send the link "
                    "to your phone. Try again after Jarvis has recognized you."
                ),
                context_data={
                    "message": (
                        "I couldn't tell who's asking, so I can't send the link "
                        "to your phone."
                    ),
                },
            )

        if not (url.startswith("http://") or url.startswith("https://")):
            logger.warning("send_link refused: invalid URL", url=url[:120])
            return CommandResponse.error_response(
                error_details="Refusing to send a non-http(s) URL.",
                context_data={
                    "message": "I don't have a valid link to send.",
                },
            )

        title = title_param or "Link from Jarvis"
        body = title if title != "Link from Jarvis" else "Tap to open"

        post_result = self._post_to_cc(
            user_id=speaker_user_id,
            url=url,
            title=title,
            body=body,
        )

        if post_result == "ok":
            logger.info("send_link delivered", user_id=speaker_user_id, url=url[:120])
            return CommandResponse.success_response(
                context_data={"message": "Sent to your phone."},
                wait_for_input=False,
            )

        # Differentiated voice messages — the prior single "I couldn't
        # reach your phone" hid three very different failure modes
        # (server unreachable, server rejected, server-said-not-sent),
        # which is the symptom that left the beta send_link incident
        # impossible to diagnose from voice alone.
        if post_result == "no_cc_url":
            message = "I can't find the server to send your link."
            details = "service_discovery returned no command-center URL"
        elif post_result == "import_error":
            message = "I can't send links — the send module isn't loaded."
            details = "ImportError in _post_to_cc — clients.rest_client or utils.service_discovery missing"
        elif post_result == "http_error":
            message = "I couldn't reach the server right now."
            details = "RestClient.post returned None (HTTP error, timeout, or unreachable)"
        else:
            message = "The server got the link but didn't deliver it to your phone."
            details = f"CC returned sent=False (post_result={post_result})"

        logger.error("send_link failed", reason=post_result, user_id=speaker_user_id)
        return CommandResponse.error_response(
            error_details=details,
            context_data={"message": message},
        )

    @staticmethod
    def _post_to_cc(user_id: int, url: str, title: str, body: str) -> str:
        """Return ``"ok"`` on success, or a short failure reason string.

        Returning a discriminated tag (instead of bool) lets ``run`` map
        each failure mode to a distinct voice response — opaque "couldn't
        reach your phone" hid the difference between "server unreachable"
        and "server rejected".
        """
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.error("send_link import failed", error=str(e))
            return "import_error"

        cc_url = get_command_center_url()
        if not cc_url:
            logger.error("send_link: get_command_center_url returned empty")
            return "no_cc_url"

        result = RestClient.post(
            f"{cc_url}/api/v0/node/send-link",
            data={
                "user_id": user_id,
                "url": url,
                "title": title,
                "body": body,
            },
            timeout=5,
        )
        if result is None:
            return "http_error"
        if not result.get("sent"):
            logger.warning("send_link: CC responded sent=False", response=str(result)[:200])
            return "not_sent"
        return "ok"
