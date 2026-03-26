"""
LightRAG 知识图谱管理器

Per-group knowledge graph instances backed by LightRAG.
Supports:
  - insert(group_id, text) → 知识入库
  - query(group_id, query) → 检索
  - warmup_instances(group_ids) → 预热

Requires ``lightrag-hku`` (optional dependency).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ...config import PluginConfig
    from ..llm_adapter import LLMAdapter

# Try importing LightRAG
try:
    from lightrag import LightRAG as _LightRAG
    from lightrag import QueryParam
    try:
        from lightrag import EmbeddingFunc
    except ImportError:
        EmbeddingFunc = None
    try:
        from lightrag.utils import wrap_embedding_func_with_attrs
    except ImportError:
        wrap_embedding_func_with_attrs = None
    HAS_LIGHTRAG = True
except ImportError:
    HAS_LIGHTRAG = False
    _LightRAG = None
    QueryParam = None
    EmbeddingFunc = None
    wrap_embedding_func_with_attrs = None


class _CompatEmbeddingWrapper:
    """Fallback wrapper matching the attrs LightRAG expects on embedding funcs."""

    def __init__(
        self,
        *,
        func: Any,
        embedding_dim: int,
        max_token_size: int,
        model_name: str,
    ) -> None:
        self.func = func
        self.embedding_dim = embedding_dim
        self.max_token_size = max_token_size
        self.model_name = model_name

    async def __call__(self, texts: list[str]) -> Any:
        return await self.func(texts)


class LightRAGKnowledgeManager:
    """Per-group LightRAG knowledge graph manager.

    Each group gets its own LightRAG instance with isolated storage.
    """

    def __init__(
        self,
        config: "PluginConfig",
        llm: Optional["LLMAdapter"] = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._instances: Dict[str, Any] = {}
        self._base_dir = getattr(
            config.knowledge, "lightrag_working_dir", "./lightrag_data"
        )
        self._lock = asyncio.Lock()
        self._retrieval_only_supported: bool | None = None

        if not HAS_LIGHTRAG:
            logger.warning(
                "[LightRAG] lightrag-hku not installed, knowledge engine disabled"
            )

    @property
    def available(self) -> bool:
        """Whether LightRAG is available."""
        return HAS_LIGHTRAG

    @staticmethod
    def _sanitize_group_id(group_id: str) -> str:
        """Sanitize group_id to prevent path traversal."""
        return re.sub(r'[^\w\-.]', '_', group_id)

    @staticmethod
    def _extract_context_result(result: Any) -> str:
        if isinstance(result, dict):
            parts: list[str] = []
            for key in ("entities", "relationships", "chunks", "context"):
                value = result.get(key)
                if value:
                    parts.append(str(value))
            return "\n\n".join(parts) if parts else ""
        return result if isinstance(result, str) else str(result)

    def _build_query_param(
        self,
        mode: str,
        retrieval_only: bool,
    ) -> Any:
        if not QueryParam:
            return None

        kwargs: dict[str, Any] = {"mode": mode}
        if retrieval_only:
            if self._retrieval_only_supported is None:
                try:
                    sig = inspect.signature(QueryParam)
                    self._retrieval_only_supported = "only_need_context" in sig.parameters
                except (TypeError, ValueError):
                    self._retrieval_only_supported = False
            if self._retrieval_only_supported:
                kwargs["only_need_context"] = True

        return QueryParam(**kwargs)

    @staticmethod
    def _supports_parameter(callable_obj: Any, name: str) -> bool:
        try:
            sig = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return False
        return name in sig.parameters

    @staticmethod
    def _flatten_history_messages(history_messages: Any) -> str:
        if not history_messages:
            return ""

        lines: list[str] = []
        for msg in history_messages:
            if not isinstance(msg, dict):
                lines.append(str(msg))
                continue

            role = str(msg.get("role", "user")).strip() or "user"
            content = msg.get("content", "")
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if text:
                            parts.append(str(text))
                    elif item:
                        parts.append(str(item))
                content_text = "\n".join(parts)
            else:
                content_text = str(content)

            content_text = content_text.strip()
            if content_text:
                lines.append(f"[{role}] {content_text}")

        return "\n".join(lines)

    def _build_embedding_func(self) -> Any:
        if self._llm is None:
            return None

        model_name = (
            getattr(self._config, "embedding_provider_id", None)
            or "astrbot-embedding"
        )

        async def _embedding_func(texts: list[str]) -> Any:
            try:
                import numpy as np
            except ImportError:  # pragma: no cover - lightrag installs numpy
                np = None

            vectors = await asyncio.gather(
                *(self._llm.get_embedding(text) for text in texts)
            )
            if any(vector is None for vector in vectors):
                raise RuntimeError(
                    "Embedding provider returned no vector for one or more texts"
                )

            if np is None:
                return vectors
            return np.asarray(vectors, dtype=float)

        if wrap_embedding_func_with_attrs is not None:
            wrapper_kwargs: dict[str, Any] = {
                "embedding_dim": self._config.embedding_dim,
                "max_token_size": 8192,
            }
            if self._supports_parameter(wrap_embedding_func_with_attrs, "model_name"):
                wrapper_kwargs["model_name"] = model_name
            elif self._supports_parameter(wrap_embedding_func_with_attrs, "model"):
                wrapper_kwargs["model"] = model_name

            return wrap_embedding_func_with_attrs(**wrapper_kwargs)(_embedding_func)

        if EmbeddingFunc is not None:
            embedding_kwargs: dict[str, Any] = {
                "embedding_dim": self._config.embedding_dim,
                "max_token_size": 8192,
                "func": _embedding_func,
            }
            if self._supports_parameter(EmbeddingFunc, "model_name"):
                embedding_kwargs["model_name"] = model_name
            elif self._supports_parameter(EmbeddingFunc, "model"):
                embedding_kwargs["model"] = model_name

            return EmbeddingFunc(**embedding_kwargs)

        return _CompatEmbeddingWrapper(
            func=_embedding_func,
            embedding_dim=self._config.embedding_dim,
            max_token_size=8192,
            model_name=model_name,
        )

    def _build_llm_model_func(self) -> Any:
        if self._llm is None:
            return None

        async def _llm_model_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, Any]] | None = None,
            keyword_extraction: bool = False,
            **kwargs: Any,
        ) -> str:
            history_text = self._flatten_history_messages(history_messages)
            final_prompt = prompt
            if history_text:
                final_prompt = (
                    "[History]\n"
                    f"{history_text}\n\n"
                    "[Current Task]\n"
                    f"{prompt}"
                )

            provider_id = (
                self._config.main_llm_provider_id
                or self._config.fast_llm_provider_id
            )
            max_tokens = kwargs.get("max_tokens", 1024)
            temperature = kwargs.get("temperature", 0.0 if keyword_extraction else 0.7)

            return await self._llm.chat_completion(
                final_prompt,
                system_prompt=system_prompt or "",
                provider_id=provider_id,
                max_tokens=max_tokens,
                temperature=temperature,
                allow_fallback=provider_id is None,
            )

        return _llm_model_func

    async def _initialize_instance(self, instance: Any) -> None:
        for method_name in ("initialize_storages", "initialize_pipeline_status"):
            method = getattr(instance, method_name, None)
            if method is None:
                continue

            maybe_coro = method()
            if inspect.isawaitable(maybe_coro):
                await maybe_coro

    async def _get_instance(self, group_id: str) -> Optional[Any]:
        """Get or create a LightRAG instance for a group."""
        if not HAS_LIGHTRAG:
            return None

        group_id = self._sanitize_group_id(group_id)

        if group_id in self._instances:
            return self._instances[group_id]

        async with self._lock:
            # Double-check after lock
            if group_id in self._instances:
                return self._instances[group_id]

            working_dir = os.path.join(self._base_dir, group_id)
            # Verify the resolved path is under base_dir to prevent traversal
            resolved = os.path.realpath(working_dir)
            base_resolved = os.path.realpath(self._base_dir)
            if not resolved.startswith(base_resolved + os.sep) and resolved != base_resolved:
                logger.error(f"[LightRAG] Path traversal attempt blocked for group_id: {group_id}")
                return None

            os.makedirs(working_dir, exist_ok=True)

            try:
                embedding_func = self._build_embedding_func()
                llm_model_func = self._build_llm_model_func()
                if embedding_func is None:
                    logger.error("[LightRAG] Instance creation failed: embedding adapter unavailable")
                    return None
                if llm_model_func is None:
                    logger.error("[LightRAG] Instance creation failed: llm adapter unavailable")
                    return None

                instance = _LightRAG(
                    working_dir=working_dir,
                    embedding_func=embedding_func,
                    llm_model_func=llm_model_func,
                )
                await self._initialize_instance(instance)
                self._instances[group_id] = instance
                logger.debug(f"[LightRAG] Instance created for {group_id}")
                return instance
            except Exception as e:
                logger.error(f"[LightRAG] Instance creation failed: {e}")
                return None

    async def insert(self, group_id: str, text: str) -> bool:
        """Insert text into a group's knowledge graph.

        Args:
            group_id: The group identifier.
            text: Text to ingest.

        Returns:
            True on success, False on failure.
        """
        group_id = self._sanitize_group_id(group_id)
        if not text or not text.strip():
            return False

        instance = await self._get_instance(group_id)
        if instance is None:
            return False

        try:
            await instance.ainsert(text)
            return True
        except Exception as e:
            logger.error(f"[LightRAG] Insert failed for {group_id}: {e}")
            return False

    async def query(
        self,
        group_id: str,
        query_text: str,
        mode: str = "mix",
        retrieval_only: bool = False,
    ) -> str:
        """Query a group's knowledge graph.

        Args:
            group_id: The group identifier.
            query_text: The query string.
            mode: LightRAG query mode ("naive", "local", "global", "hybrid", "mix").

        Returns:
            Query result text, or "" on failure.
        """
        group_id = self._sanitize_group_id(group_id)
        if not query_text or not query_text.strip():
            return ""

        instance = await self._get_instance(group_id)
        if instance is None:
            return ""

        try:
            if QueryParam:
                param = self._build_query_param(mode, retrieval_only)
                result = await instance.aquery(query_text, param=param)
            else:
                result = await instance.aquery(query_text)

            return self._extract_context_result(result)
        except Exception as e:
            logger.error(f"[LightRAG] Query failed for {group_id}: {e}")
            return ""

    async def warmup(self, group_ids: List[str]) -> None:
        """Compatibility wrapper for lifecycle warmup."""
        await self.warmup_instances(group_ids)

    async def finalize(self) -> None:
        """Flush and finalize LightRAG storages when supported."""
        for gid, instance in list(self._instances.items()):
            try:
                if hasattr(instance, "finalize_storages"):
                    await instance.finalize_storages()
                elif hasattr(instance, "flush"):
                    maybe_coro = instance.flush()
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro
            except Exception as e:
                logger.debug(f"[LightRAG] Finalize failed for {gid}: {e}")

    async def warmup_instances(self, group_ids: List[str]) -> None:
        """Pre-warm LightRAG instances for known groups."""
        for gid in group_ids:
            try:
                await self._get_instance(gid)
            except Exception as e:
                logger.debug(f"[LightRAG] Warmup failed for {gid}: {e}")

    async def close(self) -> None:
        """Release all LightRAG instances."""
        for gid, instance in self._instances.items():
            try:
                if hasattr(instance, "close"):
                    await instance.close()
                elif hasattr(instance, "finalize_storages"):
                    await instance.finalize_storages()
            except Exception:
                pass
        self._instances.clear()
        logger.debug("[LightRAG] All instances released")
