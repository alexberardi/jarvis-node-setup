"""Concrete CCHttpBackend and LLMBackend for the node runtime.

Both reach command-center as the node: the URL comes from service discovery
(``get_command_center_url``, as the commands resolved it before) and
RestClient adds the node's auth header. ``chat_text`` goes through
JarvisCommandCenterClient (CC's node-authenticated LLM passthrough). Discovery
and the client are looked up on every call, so tests that patch them still
apply.

Registered once at startup via:
    from services.cc_backend import init_cc_backend, init_llm_backend
    init_cc_backend()
    init_llm_backend()
"""

from __future__ import annotations

from typing import Any

from jarvis_command_sdk.cc_http import CCHttpBackend, set_cc_backend
from jarvis_command_sdk.llm import LLMBackend, set_llm_backend

from jarvis_log_client import JarvisLogger

from clients import jarvis_command_center_client, rest_client
from utils import service_discovery

logger = JarvisLogger(service="jarvis-node")


def _cc_url() -> str | None:
    url = service_discovery.get_command_center_url()
    if not url:
        logger.error("command-center URL not found by service discovery")
        return None
    return url.rstrip("/")


class NodeCCHttpBackend(CCHttpBackend):
    """POSTs to command-center with the node's credentials."""

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any] | None:
        cc_url = _cc_url()
        if cc_url is None:
            return None
        return rest_client.RestClient.post(f"{cc_url}/{path.lstrip('/')}", data=json)


class NodeLLMBackend(LLMBackend):
    """Text chat through command-center's node LLM passthrough."""

    def chat_text(self, prompt: str) -> str | None:
        cc_url = _cc_url()
        if cc_url is None:
            return None
        client = jarvis_command_center_client.JarvisCommandCenterClient(cc_url)
        return client.chat_text(prompt)


def init_cc_backend() -> None:
    """Register the node command-center HTTP backend with the SDK. Call once at startup."""
    set_cc_backend(NodeCCHttpBackend())


def init_llm_backend() -> None:
    """Register the node LLM backend with the SDK. Call once at startup."""
    set_llm_backend(NodeLLMBackend())
