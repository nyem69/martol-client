"""Anthropic Claude provider implementation."""

import json
import logging

import anthropic

from . import LLMProvider, LLMResponse, ToolCall
from martol_agent.tools import to_anthropic_tools

log = logging.getLogger("martol-agent")


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, model: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model or "claude-sonnet-4-20250514"

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }

        anthropic_tools = to_anthropic_tools(tools)
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        response = await self.client.messages.create(**kwargs)
        return self._parse_response(response)

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        stop_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "stop_sequence": "end_turn",
        }

        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=stop_map.get(response.stop_reason, "end_turn"),
        )

    @staticmethod
    def format_tool_result(tool_call_id: str, result: dict) -> dict:
        """Format a tool result for the Anthropic messages API."""
        return {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": json.dumps(result),
        }

    @staticmethod
    def format_assistant_message(response: LLMResponse) -> dict:
        """Format an LLMResponse as an assistant message for Anthropic."""
        content: list[dict] = []
        if response.text:
            content.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return {"role": "assistant", "content": content}
