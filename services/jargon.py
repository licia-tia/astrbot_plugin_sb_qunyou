"""
黑话统计 — 高频词提取与含义推断

学习侧：
  jieba 分词 → Counter 内存累加 → 定时 flush 到 DB (upsert)
  频率达到阈值的词 → LLM 批量推断含义 (单次最多 20 词)

注入侧：
  回复时检查消息中是否包含已知黑话 → 注入含义提示

WebUI 管理：
  用户可自定义新增/删除黑话 (is_custom=true 不被自动覆盖)
"""
from __future__ import annotations

import collections
import json
from typing import Any as _Any, TYPE_CHECKING

Any = _Any  # noqa: A001

from astrbot.api import logger

from ..config import PluginConfig

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


class JargonService:
    """Tracks high-frequency terms per group and infers meanings."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter", plugin: Any = None) -> None:
        self._config = config.jargon
        self._llm = llm
        self._plugin = plugin
        # In-memory counters: group_id → Counter
        self._counters: dict[str, collections.Counter] = {}
        self._prompts: Any = None  # lazy

    # ================================================================== #
    #  Real-time counting
    # ================================================================== #

    def count_words(self, group_id: str, text: str) -> int:
        """Tokenize with jieba and count word frequencies (in-memory).

        Returns the total count for the group (for flush threshold checks).
        """
        if not self._config.enabled:
            return 0
        try:
            import jieba
            words = jieba.lcut(text)
            # Filter: only 2+ char tokens, exclude pure digits/punctuation
            words = [
                w for w in words
                if len(w) >= 2 and not w.isdigit() and not all(
                    c in "，。！？、；：""''（）【】…—～·" for c in w
                )
            ]
            if group_id not in self._counters:
                self._counters[group_id] = collections.Counter()
            self._counters[group_id].update(words)
            return sum(self._counters[group_id].values())
        except Exception as e:
            logger.debug(f"[Jargon] jieba tokenize error: {e}")
            return 0

    # ================================================================== #
    #  DB flushing
    # ================================================================== #

    async def flush_to_db(self, group_id: str, db: "Database") -> None:
        """Flush in-memory counters to DB (PostgreSQL upsert)."""
        counter = self._counters.pop(group_id, None)
        if not counter:
            return

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            for term, freq in counter.most_common(200):
                await repo.upsert_jargon(group_id, term, frequency=freq)
            await session.commit()
        logger.info(f"[Jargon] Flushed {len(counter)} terms for {group_id}")

    async def flush_all(self, db: "Database") -> None:
        """Flush counters for all groups."""
        for gid in list(self._counters.keys()):
            await self.flush_to_db(gid, db)

    # ================================================================== #
    #  Meaning inference (batch LLM)
    # ================================================================== #

    async def infer_meanings_batch(self, group_id: str, db: "Database") -> int:
        """Batch-infer meanings for terms that lack one.

        Fetches terms above frequency threshold with empty meaning,
        sends them to LLM in one batch call, updates DB.

        Returns the number of terms updated.
        """
        if not self._config.enabled:
            return 0

        try:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                terms = await repo.get_jargon_needing_meaning(
                    group_id,
                    min_frequency=self._config.min_frequency,
                    limit=self._config.batch_infer_size,
                )

                if not terms:
                    return 0

                # Build term list for LLM
                term_list = "\n".join(
                    f"- {t.term} (出现 {t.frequency} 次)" for t in terms
                )

                # LLM batch inference
                if self._prompts is None:
                    self._prompts = getattr(self._plugin, "prompt_service", None)
                if not self._prompts:
                    return 0
                template = await self._prompts.get_prompt("JARGON_INFER_BATCH")
                prompt = template.format(terms=term_list)
                response = await self._llm.fast_chat(prompt)

                if not response:
                    return 0

                # Parse JSON response
                meanings = self._parse_meanings(response)
                if not meanings:
                    logger.warning(
                        f"[Jargon] Failed to parse meanings for {group_id}"
                    )
                    return 0

                # Update DB
                updated = 0
                for t in terms:
                    if t.term in meanings and meanings[t.term]:
                        await repo.update_jargon_meaning(t.id, meanings[t.term])
                        updated += 1

                await session.commit()
                logger.info(
                    f"[Jargon] Inferred {updated}/{len(terms)} meanings for {group_id}"
                )
                return updated

        except Exception as e:
            logger.error(f"[Jargon] Batch inference failed: {e}")
            return 0

    # ================================================================== #
    #  Retrieval (for injection)
    # ================================================================== #

    async def get_matching_jargon(
        self, group_id: str, text: str, db: "Database"
    ) -> list[tuple[str, str]]:
        """Find jargon terms present in the text. Returns (term, meaning) pairs."""
        if not self._config.enabled:
            return []

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            terms = await repo.get_group_jargon(
                group_id, min_frequency=self._config.min_frequency
            )
            matches = []
            for t in terms:
                if t.meaning and t.term in text:
                    matches.append((t.term, t.meaning))
            return matches

    # ================================================================== #
    #  Parsing helper
    # ================================================================== #

    def _parse_meanings(self, response: str) -> dict[str, str]:
        """Parse LLM response as JSON dict of term→meaning."""
        response = response.strip()

        # Try to extract from code block
        if "```" in response:
            parts = response.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    result = json.loads(part)
                    if isinstance(result, dict):
                        return {str(k): str(v) for k, v in result.items()}
                except (json.JSONDecodeError, ValueError):
                    continue

        # Direct parse
        try:
            result = json.loads(response)
            if isinstance(result, dict):
                return {str(k): str(v) for k, v in result.items()}
        except (json.JSONDecodeError, ValueError):
            pass

        return {}
