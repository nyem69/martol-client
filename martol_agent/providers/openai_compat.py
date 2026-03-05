"""OpenAI-compatible provider implementation.

Works with OpenAI, Azure OpenAI, and any OpenAI-compatible API
(Ollama, vLLM, Together, Groq, etc.) via base_url override.
"""

import json
import logging

import openai

from . import LLMProvider, LLMResponse, ToolCall
from martol_agent.tools import to_openai_tools

log = logging.getLogger("martol-agent")


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible API provider."""

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None):
        import httpx
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        self.model = model or "gpt-4o"

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        # OpenAI uses system message in the messages array
        openai_messages = [{"role": "system", "content": system}]
        openai_messages.extend(messages)

        kwargs: dict = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": 4096,
        }

        openai_tools = to_openai_tools(tools)
        if openai_tools:
            kwargs["tools"] = openai_tools

        response = await self.client.chat.completions.create(**kwargs)
        return self._parse_response(response)

    def _parse_response(
        self, response: openai.types.chat.ChatCompletion
    ) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        text = message.content
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                    )
                )

        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "end_turn",
        }

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_map.get(choice.finish_reason or "stop", "end_turn"),
        )

    @staticmethod
    def format_tool_result(tool_call_id: str, result: dict) -> dict:
        """Format a tool result for the OpenAI messages API."""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result),
        }

    @staticmethod
    def format_assistant_message(response: LLMResponse) -> dict:
        """Format an LLMResponse as an assistant message for OpenAI."""
        msg: dict = {"role": "assistant"}
        if response.text:
            msg["content"] = response.text
        if response.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
        return msg
