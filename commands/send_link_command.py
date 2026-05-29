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
from core.command_response import CommandResponse
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


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
        if speaker_user_id is None:
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

        url = (kwargs.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return CommandResponse.error_response(
                error_details="Refusing to send a non-http(s) URL.",
                context_data={
                    "message": "I don't have a valid link to send.",
                },
            )

        title = (kwargs.get("title") or "Link from Jarvis").strip() or "Link from Jarvis"
        body = title if title != "Link from Jarvis" else "Tap to open"

        sent = self._post_to_cc(
            user_id=speaker_user_id,
            url=url,
            title=title,
            body=body,
        )

        if not sent:
            return CommandResponse.error_response(
                error_details="Notification service did not accept the send.",
                context_data={
                    "message": "I couldn't reach your phone right now. Try again in a moment.",
                },
            )

        return CommandResponse.success_response(
            context_data={"message": "Sent to your phone."},
            wait_for_input=False,
        )

    @staticmethod
    def _post_to_cc(user_id: int, url: str, title: str, body: str) -> bool:
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError:
            return False

        cc_url = get_command_center_url()
        if not cc_url:
            return False

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
        return bool(result and result.get("sent"))
