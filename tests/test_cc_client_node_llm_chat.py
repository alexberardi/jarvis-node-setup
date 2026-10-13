"""chat_text/chat go through the node-authenticated /api/v0/node/llm/chat (jarvisd drops /api/v0/chat)."""

from unittest.mock import patch

from pydantic import BaseModel

from clients.jarvis_command_center_client import JarvisCommandCenterClient


class _Out(BaseModel):
    response: str


def _client() -> JarvisCommandCenterClient:
    c = JarvisCommandCenterClient.__new__(JarvisCommandCenterClient)
    c.base_url = "http://cc:7703"
    return c


def test_chat_text_posts_to_node_llm_chat():
    with patch("clients.jarvis_command_center_client.RestClient.post", return_value={"content": "hello"}) as post:
        assert _client().chat_text("prompt") == "hello"
    url, body = post.call_args[0]
    assert url == "http://cc:7703/api/v0/node/llm/chat"
    assert body == {"messages": [{"role": "system", "content": "prompt"}], "model": "live", "temperature": 0}


def test_chat_text_handles_failures():
    with patch("clients.jarvis_command_center_client.RestClient.post", return_value=None):
        assert _client().chat_text("p") is None
    with patch("clients.jarvis_command_center_client.RestClient.post", return_value={"detail": "x"}):
        assert _client().chat_text("p") is None


def test_chat_parses_model_from_content():
    with patch("clients.jarvis_command_center_client.RestClient.post", return_value={"content": '{"response": "ok"}'}):
        assert _client().chat("p", _Out) == _Out(response="ok")
    with patch("clients.jarvis_command_center_client.RestClient.post", return_value={"content": "plain words"}):
        assert _client().chat("p", _Out) == _Out(response="plain words")
