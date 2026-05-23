# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""Tests for DeepSeek provider support."""

import os
from unittest.mock import MagicMock

import pytest

from repoprover.agents.base import (
    AgentConfig,
    PROVIDER_API_KEY_ENV,
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    call_llm_simple,
    create_client,
)


class TestDeepSeekProviderTables:
    """Provider configuration tables must include deepseek entries."""

    def test_base_url(self):
        assert PROVIDER_BASE_URLS["deepseek"] == "https://api.deepseek.com"

    def test_api_key_env(self):
        assert PROVIDER_API_KEY_ENV["deepseek"] == "DEEPSEEK_API_KEY"

    def test_default_model(self):
        assert PROVIDER_DEFAULT_MODELS["deepseek"] == "deepseek-v4-pro"


class TestDeepSeekAgentConfig:
    """AgentConfig must default correctly for deepseek provider."""

    def test_default_model(self):
        config = AgentConfig(provider="deepseek")
        assert config.model == "deepseek-v4-pro"

    def test_custom_model_overrides_default(self):
        config = AgentConfig(provider="deepseek", model="deepseek-v4-flash")
        assert config.model == "deepseek-v4-flash"


class TestDeepSeekCreateClient:
    """create_client must produce a correctly configured client for deepseek."""

    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")

    def test_base_url(self):
        client = create_client(AgentConfig(provider="deepseek"))
        assert "api.deepseek.com" in str(client.base_url)

    def test_api_key_from_env(self):
        client = create_client(AgentConfig(provider="deepseek"))
        assert client.api_key == "sk-test-key"

    def test_api_key_from_config(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        client = create_client(AgentConfig(provider="deepseek", api_key="sk-config-key"))
        assert client.api_key == "sk-config-key"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            create_client(AgentConfig(provider="deepseek"))

    def test_no_anthropic_beta_header(self):
        client = create_client(AgentConfig(provider="deepseek"))
        headers = getattr(client, "_custom_headers", {}) or {}
        assert "anthropic-beta" not in headers

    def test_anthropic_still_has_beta_header(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        client = create_client(AgentConfig(provider="anthropic"))
        headers = getattr(client, "_custom_headers", {}) or {}
        assert "anthropic-beta" in headers


@pytest.mark.integration
class TestDeepSeekIntegration:
    """Integration tests requiring a real DeepSeek API key."""

    @pytest.fixture(autouse=True)
    def require_api_key(self):
        if not os.environ.get("DEEPSEEK_API_KEY"):
            pytest.skip("DEEPSEEK_API_KEY not set")

    def test_simple_chat(self):
        result = call_llm_simple(
            system="Reply with just the word 'ok'.",
            user_message="Say ok",
            config=AgentConfig(provider="deepseek", max_tokens=50),
        )
        assert result is not None
        assert len(result) > 0

    def test_call_llm_simple_returns_text(self):
        result = call_llm_simple(
            system="You are a helpful assistant.",
            user_message="What is 1+1? Answer with just the number.",
            config=AgentConfig(provider="deepseek", max_tokens=50, temperature=0),
        )
        assert "2" in result


class TestReasoningContentPreservation:
    """run_tool_loop must preserve reasoning_content in assistant messages."""

    @staticmethod
    def _make_mock_response(content, *, reasoning_content=None, tool_calls=None, finish_reason="stop"):
        """Build a mock OpenAI ChatCompletion response."""
        msg = MagicMock()
        msg.content = content
        msg.reasoning_content = reasoning_content
        msg.role = "assistant"

        if tool_calls:
            mock_tcs = []
            for tc in tool_calls:
                mock_tc = MagicMock()
                mock_tc.id = tc["id"]
                mock_tc.function.name = tc["name"]
                mock_tc.function.arguments = tc.get("arguments", "{}")
                mock_tcs.append(mock_tc)
            msg.tool_calls = mock_tcs
        else:
            msg.tool_calls = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = finish_reason

        response = MagicMock()
        response.choices = [choice]
        response.usage = None
        return response

    def test_reasoning_content_preserved_on_tool_call(self):
        """When the LLM returns reasoning_content and a tool call,
        the reasoning_content must appear in the assistant message."""
        from repoprover.agents.tools import run_tool_loop

        # Turn 1: model responds with reasoning + tool call
        # Turn 2: model responds with final answer (no tool call)
        responses = [
            self._make_mock_response(
                content="Let me check that.",
                reasoning_content="I need to look up the weather.",
                tool_calls=[{"id": "call_1", "name": "get_weather", "arguments": '{"city": "Paris"}'}],
                finish_reason="tool_calls",
            ),
            self._make_mock_response(
                content="It's sunny in Paris.",
                reasoning_content="The weather result says sunny.",
                finish_reason="stop",
            ),
        ]

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = responses

        def tool_handler(name, args):
            return "Sunny, 22C" if name == "get_weather" else "unknown"

        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
            }
        }]

        result = run_tool_loop(
            client=mock_client,
            model="deepseek-v4-pro",
            system_prompt="You are helpful.",
            initial_messages=[{"role": "user", "content": "Weather in Paris?"}],
            tools=tools,
            tool_handler=tool_handler,
            enable_compaction=False,
        )

        # Find the assistant message that has the tool call
        assistant_msgs = [m for m in result.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) >= 2

        # The first assistant message should have reasoning_content
        first = assistant_msgs[0]
        assert "reasoning_content" in first
        assert first["reasoning_content"] == "I need to look up the weather."
        assert first["tool_calls"] is not None

    def test_no_reasoning_content_when_none(self):
        """When the LLM does not return reasoning_content,
        the assistant message must NOT have the key."""
        from repoprover.agents.tools import run_tool_loop

        response = self._make_mock_response(
            content="Hello!",
            reasoning_content=None,  # Explicitly None
            finish_reason="stop",
        )

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response

        result = run_tool_loop(
            client=mock_client,
            model="gpt-4o",
            system_prompt="You are helpful.",
            initial_messages=[{"role": "user", "content": "Hi"}],
            tools=None,
            tool_handler=lambda n, a: "",
            enable_compaction=False,
        )

        assistant_msgs = [m for m in result.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert "reasoning_content" not in assistant_msgs[0]
