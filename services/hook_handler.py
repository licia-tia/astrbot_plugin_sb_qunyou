"""
LLM Hook Handler — @filter.on_llm_request() 注入处理器

注入顺序与信任分层：
1. [HIGH]   群画像           → system_prompt
2. [HIGH]   情绪             → system_prompt
3. [MEDIUM] 话题线程上下文   → extra_user_content_parts
4. [MEDIUM] 用户记忆         → extra_user_content_parts
5. [MEDIUM] 知识图谱 (LightRAG) → extra_user_content_parts
6. [LOW]    黑话解释         → extra_user_content_parts

All fetchers run concurrently with CacheManager caching.
Reranker (if available) re-orders extra_parts by relevance.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Optional, TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import ContentPart, TextPart

from ..config import PluginConfig
from ..prompts.templates import (
    INJECTION_EMOTION,
    INJECTION_GROUP_PERSONA,
    INJECTION_JARGON,
    INJECTION_PERSONA_BINDING,
    INJECTION_THREAD_CONTEXT,
    INJECTION_USER_MEMORIES,
)

if TYPE_CHECKING:
    from ..main import QunyouPlugin


class HookHandler:
    """Injects structured context into LLM requests."""

    def __init__(self, config: PluginConfig, plugin: "QunyouPlugin") -> None:
        self._config = config
        self._p = plugin

    async def handle(self, event: AstrMessageEvent, req: Any) -> None:
        """Main hook entry: gather context sources and inject into req."""
        if not req:
            return

        db = getattr(self._p, "db", None)
        if not db:
            return

        group_id = event.get_group_id() or event.get_sender_id()
        user_id = event.get_sender_id()
        message_text = event.get_message_str() or ""

        # Gather ALL context concurrently
        tasks = {
            "persona": self._fetch_persona(group_id, db),
            "emotion": self._fetch_emotion(group_id, db),
            "thread_ctx": self._fetch_thread_context(group_id, message_text, db),
            "memories": self._fetch_memories(group_id, user_id, message_text, db),
            "knowledge": self._fetch_knowledge(group_id, message_text),
            "jargon": self._fetch_jargon(group_id, message_text, db),
            "persona_binding": self._fetch_persona_binding(group_id),
        }

        results: dict[str, str] = {}
        gathered = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )
        for key, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.debug(f"[Hook] {key} fetch failed: {result}")
                results[key] = ""
            else:
                results[key] = result or ""

        # ---- Build injection ----
        system_parts: list[str] = []
        extra_parts: list[str] = []

        # HIGHEST TRUST → persona binding overrides default persona
        if results.get("persona_binding"):
            system_parts.append(results["persona_binding"])

        # HIGH TRUST → system prompt
        if results["persona"]:
            system_parts.append(
                INJECTION_GROUP_PERSONA.format(persona=results["persona"])
            )
        if results["emotion"] and results["emotion"] != "neutral":
            system_parts.append(
                INJECTION_EMOTION.format(mood=results["emotion"])
            )

        # MEDIUM TRUST → extra content
        if results["thread_ctx"]:
            extra_parts.append(results["thread_ctx"])  # already formatted
        if results["memories"]:
            extra_parts.append(
                INJECTION_USER_MEMORIES.format(
                    user_id=user_id,
                    memories=results["memories"],
                )
            )
        if results["knowledge"]:
            extra_parts.append(
                f"[知识图谱参考]\n{results['knowledge']}"
            )

        # LOW TRUST → extra content
        if results["jargon"]:
            extra_parts.append(
                INJECTION_JARGON.format(hints=results["jargon"])
            )

        # ---- Rerank extra parts ----
        if extra_parts:
            extra_parts = await self._rerank_context(
                message_text, extra_parts
            )

        # ---- Inject into request ----
        if system_parts:
            system_injection = "\n\n".join(system_parts)
            if hasattr(req, "system_prompt") and req.system_prompt:
                req.system_prompt = req.system_prompt + "\n\n" + system_injection
            elif hasattr(req, "system_prompt"):
                req.system_prompt = system_injection

        if extra_parts:
            extra_injection = "\n\n".join(extra_parts)
            if hasattr(req, "extra_user_content_parts"):
                req.extra_user_content_parts = self._normalize_extra_parts(
                    getattr(req, "extra_user_content_parts", None)
                )
                req.extra_user_content_parts.append(TextPart(text=extra_injection))

    # ------------------------------------------------------------------ #
    #  Context fetchers (each returns str, never raises)
    # ------------------------------------------------------------------ #

    async def _fetch_persona(self, group_id: str, db: Any) -> str:
        try:
            return await self._p.group_persona.get_group_prompt(group_id, db)
        except Exception:
            return ""

    async def _fetch_emotion(self, group_id: str, db: Any) -> str:
        if not self._config.emotion.enabled:
            return ""
        try:
            return await self._p.emotion.get_mood(group_id, db)
        except Exception:
            return ""

    async def _fetch_thread_context(
        self, group_id: str, text: str, db: Any
    ) -> str:
        """Find the best-matching thread and fetch its recent messages."""
        if not self._config.topic.enabled:
            return ""
        try:
            topic_router = getattr(self._p, "topic_router", None)
            if not topic_router:
                return ""

            embedding = await self._p.llm.get_embedding(text)
            if embedding is None:
                return ""

            # Find matching thread without creating new one
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                threads = await repo.get_active_threads(
                    group_id, limit=self._config.topic.max_threads_per_group
                )

                if not threads:
                    return ""

                # Find best match
                from ..pipeline.topic_router import _cosine_sim
                best_thread = None
                best_sim = -1.0
                for t in threads:
                    if t.centroid is not None:
                        sim = _cosine_sim(embedding, list(t.centroid))
                        if sim > best_sim:
                            best_sim = sim
                            best_thread = t

                if not best_thread or best_sim < self._config.topic.similarity_threshold * 0.8:
                    return ""

                # Fetch thread messages
                messages = await repo.get_thread_messages(best_thread.id, limit=10)
                if not messages:
                    return ""

                topic = best_thread.topic_summary or "ongoing"
                lines = []
                for m in reversed(messages):
                    name = m.sender_name or m.sender_id
                    lines.append(f"[{name}]: {m.text}")

                return INJECTION_THREAD_CONTEXT.format(
                    topic=topic,
                    messages="\n".join(lines),
                )

        except Exception as e:
            logger.debug(f"[Hook] Thread context failed: {e}")
            return ""

    async def _fetch_memories(
        self, group_id: str, user_id: str, text: str, db: Any
    ) -> str:
        """Retrieve relevant user memories for context injection."""
        try:
            speaker_mem = getattr(self._p, "speaker_memory", None)
            if not speaker_mem:
                return ""

            facts = await speaker_mem.retrieve_relevant(
                group_id, user_id, text, db
            )
            if not facts:
                return ""

            return "\n".join(f"- {f}" for f in facts)
        except Exception:
            return ""

    async def _fetch_jargon(self, group_id: str, text: str, db: Any) -> str:
        if not self._config.jargon.enabled:
            return ""
        try:
            # Check cache first
            cache = self._get_cache()
            if cache:
                text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                cached = cache.get("context", f"jargon:{group_id}:{text_hash}")
                if cached is not None:
                    return cached

            matches = await self._p.jargon.get_matching_jargon(group_id, text, db)
            if not matches:
                return ""
            result = "\n".join(f"「{t}」= {m}" for t, m in matches)

            if cache:
                text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                cache.set("context", f"jargon:{group_id}:{text_hash}", result)
            return result
        except Exception:
            return ""

    async def _fetch_knowledge(
        self, group_id: str, text: str
    ) -> str:
        """Query LightRAG knowledge graph for relevant context."""
        knowledge = getattr(self._p, "knowledge", None)
        if not knowledge:
            return ""
        try:
            # Check cache first
            cache = self._get_cache()
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            cache_key = f"knowledge:{group_id}:{text_hash}"
            if cache:
                cached = cache.get("knowledge", cache_key)
                if cached is not None:
                    return cached

            result = await knowledge.query(
                group_id, text,
                mode=self._config.knowledge.query_mode,
                retrieval_only=self._config.knowledge.retrieval_only_query_preferred,
            )

            if cache and result:
                cache.set("knowledge", cache_key, result)
            return result
        except Exception as e:
            logger.debug(f"[Hook] Knowledge fetch failed: {e}")
            return ""

    async def _fetch_persona_binding(self, group_id: str) -> str:
        """Fetch bound persona prompt + active learned tone for injection."""
        persona_binding_svc = getattr(self._p, "persona_binding", None)
        if not persona_binding_svc:
            return ""

        db = getattr(self._p, "db", None)
        if not db:
            return ""

        try:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                binding, tone_text = await repo.get_persona_binding_with_active_tone(group_id)

                if not binding or not binding.bound_persona_id:
                    return ""

            # PersonaManager lookup — no DB needed
            persona_prompt = await persona_binding_svc.get_persona_prompt_by_id(
                binding.bound_persona_id, self._p.context
            )

            if not persona_prompt and not tone_text:
                return ""

            from ..prompts.templates import INJECTION_PERSONA_BINDING
            return INJECTION_PERSONA_BINDING.format(
                persona_prompt=persona_prompt or "",
                tone=tone_text or "(尚未学习语气)",
            )
        except Exception as e:
            logger.debug(f"[Hook] Persona binding fetch failed: {e}")
            return ""

    # ------------------------------------------------------------------ #
    #  Reranker
    # ------------------------------------------------------------------ #

    async def _rerank_context(
        self, query: str, parts: list[str]
    ) -> list[str]:
        """Re-order context parts by relevance using Reranker."""
        reranker = getattr(self._p, "reranker", None)
        if not reranker or not self._config.rerank.enabled:
            return parts  # passthrough

        if len(parts) <= 1:
            return parts  # nothing to rerank

        try:
            results = await reranker.rerank(
                query, parts,
                top_n=min(self._config.rerank.top_k, len(parts)),
            )
            # Rebuild in reranked order
            reranked = []
            for r in results:
                if 0 <= r.index < len(parts):
                    reranked.append(parts[r.index])
            return reranked if reranked else parts
        except Exception as e:
            logger.debug(f"[Hook] Rerank failed: {e}")
            return parts  # fallback to original order

    # ------------------------------------------------------------------ #
    #  Cache helper
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_cache():
        """Get CacheManager if available."""
        try:
            from ..utils.cache import get_cache_manager
            return get_cache_manager()
        except Exception:
            return None

    @staticmethod
    def _normalize_extra_parts(parts: Any) -> list[ContentPart]:
        if not parts:
            return []

        normalized: list[ContentPart] = []
        for part in parts:
            if isinstance(part, ContentPart):
                normalized.append(part)
                continue

            if isinstance(part, dict):
                try:
                    normalized.append(ContentPart.model_validate(part))
                    continue
                except Exception:
                    pass

            normalized.append(TextPart(text=str(part)))

        return normalized
