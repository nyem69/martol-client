"""LLM provider abstraction for multi-provider AI integration."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """A tool call requested by the LLM."""

    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"  # "end_turn", "tool_use", "max_tokens"


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """Send a chat request to the LLM.

        Args:
            system: System prompt.
            messages: Conversation messages in provider-native format.
            tools: Tool definitions in canonical format (from martol_agent.tools).

        Returns:
            Normalized LLMResponse with text and/or tool calls.
        """
        ...

    @abstractmethod
    async def stream_chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> AsyncIterator[str | LLMResponse]:
        """Stream a chat response. Yields str deltas, then a final LLMResponse."""
        ...
        # Make this a valid async generator for type checking
        yield  # type: ignore  # pragma: no cover


def create_provider(
    provider: str,
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """Factory function to create an LLM provider instance.

    Args:
        provider: Provider name ("anthropic" or "openai").
        api_key: API key for the provider.
        model: Model ID override (uses provider default if None).
        base_url: Base URL override (OpenAI-compatible only).
    """
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(api_key, model)
    elif provider == "openai":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(api_key, model, base_url)
    raise ValueError(f"Unknown provider: {provider!r}. Use 'anthropic' or 'openai'.")
