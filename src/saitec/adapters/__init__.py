"""adapters — Layer 3

LLM 协议适配层（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages）。
"""
from __future__ import annotations

from .base import Adapter
from .anthropic_messages import AnthropicMessagesAdapter
from .openai_chat_completions import OpenAIChatCompletionsAdapter
from .openai_responses import OpenAIResponsesAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    "openai-chat-completions": OpenAIChatCompletionsAdapter,
    "openai-responses": OpenAIResponsesAdapter,
    "anthropic-messages": AnthropicMessagesAdapter,
}


def get_adapter(endpoint_type: str) -> Adapter:
    """按 endpoint_type 获取对应 adapter 实例"""
    cls = _REGISTRY.get(endpoint_type)
    if cls is None:
        raise ValueError(f"unsupported endpoint_type: {endpoint_type}")
    return cls()


__all__ = [
    "Adapter",
    "OpenAIChatCompletionsAdapter",
    "OpenAIResponsesAdapter",
    "AnthropicMessagesAdapter",
    "get_adapter",
]