"""
话题线程路由 — 多线程话题池解决鸡尾酒会问题

核心算法：
  1. 获取 group 的活跃线程列表
  2. 计算消息 embedding 与每个线程 centroid 的余弦相似度
  3. max_sim >= threshold → 归入该线程 (更新 centroid 为滑动平均)
  4. max_sim < threshold → 创建新线程
  5. 过期线程自动归档
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional, TYPE_CHECKING

import numpy as np
from astrbot.api import logger

from ..config import TopicConfig

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    dot = float(np.dot(va, vb))
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return dot / norm if norm > 0 else 0.0


def _sliding_average(
    old: list[float], new: list[float], alpha: float = 0.1
) -> list[float]:
    """Compute exponential moving average of centroid with new vector."""
    va = np.array(old, dtype=np.float32)
    vn = np.array(new, dtype=np.float32)
    result = va * (1 - alpha) + vn * alpha
    return result.tolist()


class TopicThreadRouter:
    """Routes messages to topic threads based on embedding similarity."""

    def __init__(self, config: TopicConfig, llm: "LLMAdapter") -> None:
        self._config = config
        self._llm = llm

    async def route_message(
        self,
        group_id: str,
        message_id: int,
        text: str,
        db: "Database",
    ) -> Optional[int]:
        """Route a message to a thread. Returns thread_id.

        Returns None if topic routing is disabled or embedding fails.
        """
        if not self._config.enabled:
            return None

        # Compute message embedding
        embedding = await self._llm.get_embedding(text)
        if embedding is None:
            return None

        # Also store the embedding on the raw message
        try:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                await repo.update_message_embedding(message_id, embedding)
                await session.commit()
        except Exception:
            pass  # non-critical

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)

            # Archive stale threads first
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
                minutes=self._config.thread_ttl_minutes
            )
            archived = await repo.archive_stale_threads(group_id, cutoff)
            if archived:
                logger.debug(f"[TopicRouter] Archived {archived} stale thread(s)")

            # Get active threads
            threads = await repo.get_active_threads(
                group_id, limit=self._config.max_threads_per_group
            )

            # Find best matching thread
            best_thread = None
            best_sim = -1.0

            for t in threads:
                if t.centroid is not None:
                    sim = _cosine_sim(embedding, list(t.centroid))
                    if sim > best_sim:
                        best_sim = sim
                        best_thread = t

            now = _dt.datetime.now(_dt.timezone.utc)

            if best_thread and best_sim >= self._config.similarity_threshold:
                # Join existing thread
                new_count = best_thread.message_count + 1
                new_centroid = _sliding_average(
                    list(best_thread.centroid), embedding, self._config.centroid_ema_alpha
                )
                await repo.update_thread(
                    best_thread.id,
                    centroid=new_centroid,
                    message_count=new_count,
                    last_activity=now,
                )
                await repo.add_message_to_thread(message_id, best_thread.id)
                await session.commit()
                logger.debug(
                    f"[TopicRouter] Message {message_id} → Thread {best_thread.id} "
                    f"(sim={best_sim:.3f})"
                )

                # Auto-update thread summary every N messages
                if new_count % self._config.summary_interval == 0:
                    await self._update_thread_summary(best_thread.id, db)

                return best_thread.id
            else:
                # Create new thread
                thread = await repo.create_thread(
                    group_id, topic_summary="", centroid=embedding
                )
                await repo.update_thread(
                    thread.id,
                    message_count=1,
                    last_activity=now,
                )
                await repo.add_message_to_thread(message_id, thread.id)
                await session.commit()
                logger.debug(
                    f"[TopicRouter] Message {message_id} → NEW Thread {thread.id}"
                )
                return thread.id

    async def _update_thread_summary(self, thread_id: int, db: "Database") -> None:
        """Auto-update thread topic summary via fast LLM."""
        try:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                messages = await repo.get_thread_messages(thread_id, limit=15)

                if not messages:
                    return

                lines = []
                for m in reversed(messages):
                    name = m.sender_name or m.sender_id
                    lines.append(f"[{name}]: {m.text}")

                from ..prompts.templates import THREAD_SUMMARY
                prompt = THREAD_SUMMARY.format(messages="\n".join(lines))
                summary = await self._llm.fast_chat(prompt)

                if summary and len(summary.strip()) > 2:
                    await repo.update_thread(
                        thread_id, topic_summary=summary.strip()[:100]
                    )
                    await session.commit()
                    logger.debug(
                        f"[TopicRouter] Thread {thread_id} summary: {summary.strip()[:50]}"
                    )
        except Exception as e:
            logger.debug(f"[TopicRouter] Summary update failed: {e}")

    async def get_thread_context(
        self,
        thread_id: int,
        db: "Database",
        limit: int = 10,
    ) -> list[dict]:
        """Retrieve recent messages in a thread for context injection."""
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            messages = await repo.get_thread_messages(thread_id, limit=limit)
            return [
                {
                    "sender": m.sender_name or m.sender_id,
                    "text": m.text,
                    "timestamp": str(m.timestamp),
                }
                for m in reversed(messages)  # chronological order
            ]
