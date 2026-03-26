"""
LLM Hook Handler — @filter.on_llm_request() 注入处理器

运行时分为两条注入链路：
1. 每群人格：精确替换 system_prompt 中的 <qunyou_persona_slot>
2. 补充上下文：群画像、线程、记忆、知识、黑话等追加到 extra_user_content_parts

All fetchers run concurrently with CacheManager caching.
Reranker (if available) re-orders extra_parts by relevance.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import re
from typing import Any, Optional, TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.message import ContentPart, TextPart

from ..config import PluginConfig


# -------------------------------------------------------------------------- #
#  System Prompt Slot Rewriter
# -------------------------------------------------------------------------- #

_PERSONA_SLOT_RE = re.compile(r'<qunyou_persona_slot>\s*(.*?)\s*</qunyou_persona_slot>', re.DOTALL)


def _rewrite_system_prompt(
    original: str,
    combined_content: str,
) -> tuple[str, bool]:
    """Replace the <qunyou_persona_slot> tag with the effective per-group persona.

    Args:
        original: The original AstrBot system prompt.
        combined_content: The effective per-group persona prompt to inject.

    Returns:
        (rewritten_prompt, slot_found).
        If the slot tag is not found, returns (original, False) and logs a warning.
    """
    slot_match = _PERSONA_SLOT_RE.search(original)

    if not slot_match:
        logger.warning(
            "[Hook] System prompt has no <qunyou_persona_slot> tag. "
            "Persona binding will NOT be injected. "
            "Add this tag to your AstrBot persona prompt to enable injection."
        )
        return original, False

    rewritten = _PERSONA_SLOT_RE.sub(combined_content.strip(), original)
    return rewritten, True

if TYPE_CHECKING:
    from ..main import QunyouPlugin


class HookHandler:
    """Injects structured context into LLM requests."""

    def __init__(self, config: PluginConfig, plugin: "QunyouPlugin") -> None:
        self._config = config
        self._p = plugin
        self._prompts: Any = None  # lazy, set on first handle()
        self._ctx: Any = None  # lazy, set on first handle()

    def _debug_enabled(self) -> bool:
        return bool(getattr(getattr(self._config, "debug", None), "enabled", False))

    def _debug_print(self, title: str, details: str) -> None:
        if not self._debug_enabled():
            return
        print(f"[HookDebug] {title}\n{details}")

    def _record_slot_status(self, group_id: str, system_prompt: str) -> None:
        slot_found = bool(system_prompt and _PERSONA_SLOT_RE.search(system_prompt))
        status_map = getattr(self._p, "_prompt_slot_status", None)
        if status_map is None:
            status_map = {}
            setattr(self._p, "_prompt_slot_status", status_map)
        status_map[group_id] = {
            "has_persona_slot": slot_found,
            "checked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "system_prompt_length": len(system_prompt or ""),
        }

    async def handle(self, event: AstrMessageEvent, req: Any) -> None:
        """Main hook entry: gather context sources and inject into req."""
        if not req:
            return

        # Lazy init: services are ready after on_load()
        if self._prompts is None:
            self._prompts = getattr(self._p, "prompt_service", None)
        if self._ctx is None:
            self._ctx = getattr(self._p, "context_builder", None)

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

        results: dict[str, Any] = {}
        gathered = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )
        for key, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.debug(f"[Hook] {key} fetch failed: {result}")
                results[key] = ""
            else:
                results[key] = result or ""

        if hasattr(req, "extra_user_content_parts"):
            req.extra_user_content_parts = self._normalize_extra_parts(
                getattr(req, "extra_user_content_parts", None)
            )

        # ---- Build injection ----
        stable_extra_parts: list[str] = []
        rerankable_extra_parts: list[str] = []

        # ---- Persona binding: slot-based rewrite ----
        persona_binding_result = results.get("persona_binding") or ""
        effective_persona = persona_binding_result.strip() if isinstance(persona_binding_result, str) else ""
        original_sp = getattr(req, "system_prompt", "") or ""
        self._record_slot_status(group_id, original_sp)

        if effective_persona:
            rewritten_sp, ok = _rewrite_system_prompt(original_sp, effective_persona)
            if ok:
                req.system_prompt = rewritten_sp
            else:
                # Slot not found: preserve original system_prompt untouched
                pass
        else:
            # No persona binding: preserve original system_prompt unchanged
            if hasattr(req, "system_prompt") and original_sp:
                req.system_prompt = original_sp

        if results.get("emotion") and results["emotion"] != "neutral":
            emotion_inj = await self._ctx.build_emotion_injection(results["emotion"])
            stable_extra_parts.append(emotion_inj)

        # ---- HIGH TRUST: group persona → extra_user_content_parts (not system_prompt) ----
        # Group persona is now always in extra_parts to avoid double-injection
        # when slots are present; it stays as supplementary context
        if results.get("persona"):
            persona_inj = await self._ctx.build_persona_injection(results["persona"])
            stable_extra_parts.append(persona_inj)

        # ---- MEDIUM TRUST → extra content ----
        if results.get("thread_ctx"):
            rerankable_extra_parts.append(results["thread_ctx"])
        if results.get("memories"):
            raw_lines = [l.strip() for l in results["memories"].strip().split("\n") if l.strip()]
            facts = [l[2:] if l.startswith("- ") else l for l in raw_lines]
            mem_inj = await self._ctx.build_memory_injection(user_id, facts)
            rerankable_extra_parts.append(mem_inj)
        if results.get("knowledge"):
            rerankable_extra_parts.append(f"[知识图谱参考]\n{results['knowledge']}")

        # LOW TRUST → extra content
        if results.get("jargon"):
            jargon_template = await self._prompts.get_prompt("INJECTION_JARGON")
            rerankable_extra_parts.append(jargon_template.format(hints=results["jargon"]))

        # ---- Rerank extra parts ----
        if rerankable_extra_parts:
            rerankable_extra_parts = await self._rerank_context(message_text, rerankable_extra_parts)

        extra_parts = stable_extra_parts + rerankable_extra_parts

        # ---- Inject extra parts ----
        if extra_parts:
            extra_injection = "\n\n".join(extra_parts)
            if hasattr(req, "extra_user_content_parts"):
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

                msgs_for_ctx = [
                    {"sender": m.sender_name or m.sender_id, "text": m.text}
                    for m in reversed(messages)
                ]
                return await self._ctx.build_thread_injection(topic, msgs_for_ctx)

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
                self._debug_print(
                    "Memory Search",
                    f"group_id={group_id}\nuser_id={user_id}\nquery={text}\nstatus=speaker_memory_unavailable",
                )
                return ""

            facts = await speaker_mem.retrieve_relevant(
                group_id, user_id, text, db
            )
            if not facts:
                self._debug_print(
                    "Memory Search",
                    f"group_id={group_id}\nuser_id={user_id}\nquery={text}\nstatus=no_hits",
                )
                return ""

            self._debug_print(
                "Memory Search",
                (
                    f"group_id={group_id}\nuser_id={user_id}\nquery={text}\n"
                    f"hits={len(facts)}\ncontent=\n" + "\n".join(facts)
                ),
            )
            return "\n".join(f"- {f}" for f in facts)
        except Exception as e:
            self._debug_print(
                "Memory Search",
                f"group_id={group_id}\nuser_id={user_id}\nquery={text}\nstatus=error\nerror={e}",
            )
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
            self._debug_print(
                "RAG Search",
                f"group_id={group_id}\nquery={text}\nstatus=knowledge_unavailable",
            )
            return ""
        try:
            # Check cache first
            cache = self._get_cache()
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            cache_key = f"knowledge:{group_id}:{text_hash}"
            if cache:
                cached = cache.get("knowledge", cache_key)
                if cached is not None:
                    self._debug_print(
                        "RAG Search",
                        f"group_id={group_id}\nquery={text}\nsource=cache\ncontent=\n{cached or '(empty)'}",
                    )
                    return cached

            result = await knowledge.query(
                group_id, text,
                mode=self._config.knowledge.query_mode,
                retrieval_only=self._config.knowledge.retrieval_only_query_preferred,
            )

            self._debug_print(
                "RAG Search",
                (
                    f"group_id={group_id}\nquery={text}\nsource=query\n"
                    f"status={'hit' if result else 'no_hits'}\ncontent=\n{result or '(empty)'}"
                ),
            )
            if cache and result:
                cache.set("knowledge", cache_key, result)
            return result
        except Exception as e:
            self._debug_print(
                "RAG Search",
                f"group_id={group_id}\nquery={text}\nstatus=error\nerror={e}",
            )
            logger.debug(f"[Hook] Knowledge fetch failed: {e}")
            return ""

    async def _fetch_persona_binding(self, group_id: str) -> str:
        """Fetch the effective per-group persona text for slot replacement."""
        persona_binding_svc = getattr(self._p, "persona_binding", None)
        if not persona_binding_svc:
            return ""

        db = getattr(self._p, "db", None)
        if not db:
            return ""

        try:
            effective_persona, _source = await persona_binding_svc.resolve_effective_persona_prompt(
                group_id, db
            )
            return effective_persona or ""
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
