"""Generic LLM-call primitive for node-side code (agents, commands, services).

Calls jarvis-command-center's `/api/v0/node/llm/chat`, which proxies to the
configured LLM proxy backend. This is intentionally a thin passthrough —
callers own the prompt, the parsing, and the decision. No domain logic
(notification gating, classification, summarization, etc.) lives here; each
agent builds whatever it needs on top.

Nodes never talk to the LLM proxy directly. All LLM traffic flows through CC.
"""

from typing import Optional

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def ask_llm(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: str = "live",
    temperature: float = 0.0,
    timeout: int = 15,
) -> Optional[str]:
    """Send a one-shot prompt to CC's LLM passthrough and return the text.

    Returns the model's response string, or ``None`` if the call failed
    (transport error, CC unreachable, malformed response). Callers decide
    how to handle ``None`` — typically fail open / proceed with the
    "no filter applied" branch.

    For multi-turn or tool-using conversations, build the messages array
    yourself and POST to /api/v0/node/llm/chat directly via RestClient.
    """
    cc_url: str = get_command_center_url() or ""
    if not cc_url:
        logger.warning("ask_llm skipped — command center URL not configured")
        return None

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    result = RestClient.post(
        f"{cc_url.rstrip('/')}/api/v0/node/llm/chat",
        data={
            "messages": messages,
            "model": model,
            "temperature": temperature,
        },
        timeout=timeout,
    )

    if result is None:
        logger.warning("ask_llm transport failed")
        return None

    content = result.get("content")
    if not isinstance(content, str):
        logger.warning("ask_llm response missing content", result=str(result)[:200])
        return None

    return content
