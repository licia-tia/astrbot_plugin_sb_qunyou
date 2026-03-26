"""
发言认知 — 用户记忆服务 (Mem0-style, PostgreSQL + pgvector only)

学习侧：
  每条消息 → LLM 抽取事实 → compute embedding → 存入 user_memories

检索侧：
  回复特定用户时 → compute query embedding → pgvector 近邻检索 → top-k 注入
"""
from __future__ import annotations

import json
from typing import Any as _Any, TYPE_CHECKING

Any = _Any  # noqa: A001

from astrbot.api import logger

from ..config import PluginConfig
from ..constants import MEMORY_MAX_FACTS_PER_MESSAGE, MEMORY_TOP_K

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


class SpeakerMemoryService:
    """Manages per-user memories using pgvector for retrieval."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter", plugin: Any = None) -> None:
        self._config = config
        self._llm = llm
        self._plugin = plugin
        self._prompts: Any = None  # lazy

    async def extract_and_store(
        self,
        group_id: str,
        user_id: str,
        sender_name: str,
        message: str,
        db: "Database",
    ) -> None:
        """Extract facts from a message and store as user memories.

        Only processes messages long enough to potentially contain facts.
        """
        # Skip very short messages — unlikely to contain memorable facts
        if len(message.strip()) < 15:
            return

        try:
            # 1. LLM extract facts
            # Truncate to prevent prompt injection via extremely long messages
            message_truncated = message[:2000] if len(message) > 2000 else message
            if self._prompts is None:
                self._prompts = getattr(self._plugin, "prompt_service", None)
            if not self._prompts:
                return
            template = await self._prompts.get_prompt("MEMORY_EXTRACT")
            prompt = template.format(
                sender_name=(sender_name or user_id)[:50],
                message=message_truncated,
            )
            response = await self._llm.fast_chat(prompt)
            if not response:
                return

            # 2. Parse JSON array from response
            facts = self._parse_facts(response)
            if not facts:
                return

            # Cap at max facts per message
            facts = facts[:MEMORY_MAX_FACTS_PER_MESSAGE]

            # 3. Compute embeddings and store
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)

                for fact in facts:
                    if not fact or len(fact.strip()) < 5:
                        continue

                    embedding = await self._llm.get_embedding(fact)
                    await repo.add_user_memory(
                        group_id=group_id,
                        user_id=user_id,
                        fact=fact.strip(),
                        importance=0.5,
                        embedding=embedding,
                    )

                await session.commit()

            logger.debug(
                f"[SpeakerMemory] Stored {len(facts)} fact(s) for "
                f"{sender_name}({user_id}) in {group_id}"
            )

        except Exception as e:
            logger.debug(f"[SpeakerMemory] Extract failed: {e}")

    async def retrieve_relevant(
        self,
        group_id: str,
        user_id: str,
        query: str,
        db: "Database",
        top_k: int = MEMORY_TOP_K,
    ) -> list[str]:
        """Retrieve user memories relevant to the query via pgvector search."""
        try:
            query_embedding = await self._llm.get_embedding(query)
            if query_embedding is None:
                return []

            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                memories = await repo.search_user_memories(
                    group_id=group_id,
                    user_id=user_id,
                    query_embedding=query_embedding,
                    top_k=top_k,
                )
                return [m.fact for m in memories]

        except Exception as e:
            logger.debug(f"[SpeakerMemory] Retrieve failed: {e}")
            return []

    def _parse_facts(self, response: str) -> list[str]:
        """Parse LLM response as JSON array of fact strings."""
        response = response.strip()

        # Try to extract JSON from markdown code block
        if "```" in response:
            parts = response.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    result = json.loads(part)
                    if isinstance(result, list):
                        return [str(x) for x in result if x]
                except (json.JSONDecodeError, ValueError):
                    continue

        # Try direct JSON parse
        try:
            result = json.loads(response)
            if isinstance(result, list):
                return [str(x) for x in result if x]
        except (json.JSONDecodeError, ValueError):
            pass

        return []
