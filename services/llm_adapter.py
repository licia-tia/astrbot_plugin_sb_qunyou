"""
AstrBot LLM Provider 桥接层

Provides two core methods:
  - chat_completion(messages, ...) → str
  - get_embedding(text) → list[float]

Wraps AstrBot's framework Provider system so that the rest of the plugin
never touches framework internals directly.
"""
from __future__ import annotations

from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.star import Context

from ..config import PluginConfig


class LLMAdapter:
    """Bridge to AstrBot framework LLM providers."""

    def __init__(self, context: Context, config: PluginConfig) -> None:
        self._ctx = context
        self._config = config

    # ------------------------------------------------------------------ #
    #  Internal: resolve provider by ID
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_provider_id(provider_id: Optional[str]) -> Optional[str]:
        if provider_id is None:
            return None
        normalized = provider_id.strip()
        return normalized or None

    def _get_provider(
        self,
        provider_id: Optional[str],
        *,
        allow_fallback: bool = True,
    ) -> Any:
        """Resolve an AstrBot LLM provider by ID.

        Falls back to the first available provider if not specified.
        """
        pid = self._normalize_provider_id(provider_id)
        if not pid:
            if not allow_fallback:
                return None
            # attempt to find any available provider
            providers = getattr(self._ctx, "get_all_providers", None)
            if providers:
                all_p = providers()
                if all_p:
                    return all_p[0]
            return None

        get_fn = getattr(self._ctx, "get_provider_by_id", None)
        if get_fn:
            return get_fn(pid)
        return None

    async def _chat_with_configured_provider(
        self,
        prompt: str,
        *,
        system_prompt: str,
        provider_id: Optional[str],
        provider_label: str,
    ) -> str:
        pid = self._normalize_provider_id(provider_id)
        if pid is None:
            logger.warning(f"[LLMAdapter] {provider_label} is not configured")
            return ""
        return await self.chat_completion(
            prompt,
            system_prompt=system_prompt,
            provider_id=pid,
            allow_fallback=False,
        )

    # ------------------------------------------------------------------ #
    #  Chat completion
    # ------------------------------------------------------------------ #

    async def chat_completion(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        provider_id: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        response_format: Optional[dict] = None,
        allow_fallback: bool = True,
    ) -> str:
        """Send a chat completion request and return the assistant message.

        Args:
            prompt: The user message.
            system_prompt: Optional system prompt.
            provider_id: Specific provider ID. If None, uses fast_llm_provider_id.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            response_format: Optional response format (e.g. {"type": "json_object"}).
            allow_fallback: If True, may fall back to the first available provider.

        Returns:
            The assistant response text, or "" on failure.
        """
        pid = self._normalize_provider_id(provider_id)
        if pid is None:
            pid = self._normalize_provider_id(self._config.fast_llm_provider_id)
        provider = self._get_provider(
            pid,
            allow_fallback=allow_fallback and pid is None,
        )
        if provider is None:
            logger.warning(f"[LLMAdapter] No provider available (requested: {pid})")
            return ""

        try:
            # Build the request using AstrBot's provider API
            llm_instance = getattr(provider, "llm", provider)
            text_completion = getattr(llm_instance, "text_chat", None)

            if text_completion is None:
                logger.warning("[LLMAdapter] Provider has no text_chat method")
                return ""

            # Build kwargs, forwarding supported parameters
            kwargs = {
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
            if max_tokens != 1024:  # Only pass if non-default
                kwargs["max_tokens"] = max_tokens
            if temperature != 0.7:
                kwargs["temperature"] = temperature
            if response_format:
                kwargs["response_format"] = response_format

            try:
                result = await text_completion(**kwargs)
            except TypeError:
                # Provider may not support all kwargs — fall back to basic call
                result = await text_completion(
                    prompt=prompt,
                    system_prompt=system_prompt,
                )
            if result and hasattr(result, "completion_text"):
                return result.completion_text or ""
            if isinstance(result, str):
                return result
            return str(result) if result else ""

        except Exception as e:
            logger.error(f"[LLMAdapter] chat_completion failed: {e}")
            return ""

    async def fast_chat(self, prompt: str, system_prompt: str = "") -> str:
        """Shortcut for fast/cheap model completion.

        This path is strict: it only uses the configured fast provider.
        """
        return await self._chat_with_configured_provider(
            prompt,
            system_prompt=system_prompt,
            provider_id=self._config.fast_llm_provider_id,
            provider_label="fast_llm_provider_id",
        )

    async def main_chat(self, prompt: str, system_prompt: str = "") -> str:
        """Shortcut for main/powerful model completion.

        This path is strict: it only uses the configured main provider.
        """
        return await self._chat_with_configured_provider(
            prompt,
            system_prompt=system_prompt,
            provider_id=self._config.main_llm_provider_id,
            provider_label="main_llm_provider_id",
        )

    # ------------------------------------------------------------------ #
    #  Embedding
    # ------------------------------------------------------------------ #

    async def get_embedding(self, text: str) -> Optional[list[float]]:
        """Compute an embedding vector for the given text.

        Returns None on failure.
        """
        pid = self._normalize_provider_id(self._config.embedding_provider_id)
        if pid is None:
            logger.warning("[LLMAdapter] embedding_provider_id is not configured")
            return None

        provider = self._get_provider(pid, allow_fallback=False)
        if provider is None:
            logger.warning(f"[LLMAdapter] No embedding provider (requested: {pid})")
            return None

        try:
            embed_fn = getattr(provider, "get_embeddings", None)
            if embed_fn is None:
                # Try alternate API shapes
                llm = getattr(provider, "llm", provider)
                embed_fn = getattr(llm, "get_embeddings", None)

            if embed_fn is None:
                logger.warning("[LLMAdapter] Provider has no get_embeddings method")
                return None

            result = await embed_fn([text])
            if result and len(result) > 0:
                return result[0]
            return None

        except Exception as e:
            logger.error(f"[LLMAdapter] get_embedding failed: {e}")
            return None
