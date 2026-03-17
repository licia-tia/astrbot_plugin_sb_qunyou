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
    HAS_LIGHTRAG = True
except ImportError:
    HAS_LIGHTRAG = False
    _LightRAG = None
    QueryParam = None


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
                # Create LightRAG instance with embedding via our LLM adapter
                instance = _LightRAG(working_dir=working_dir)
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
