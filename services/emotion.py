"""
情绪引擎 — 动态情绪状态机

V1: 离散情绪标签 (happy/neutral/angry/sad/excited/bored)
  - 对话触发情绪更新：LLM 分析 → 概率门控 → 更新 DB
  - sensitivity 控制变化概率：0=几乎不变 1=每次都变
  - 情绪自然衰减：超过 decay_hours 自动回归 neutral

V2 (预留): Valence-Arousal 连续模型
"""
from __future__ import annotations

import datetime as _dt
import random
from typing import Any as _Any, Optional, TYPE_CHECKING

Any = _Any  # noqa: A001

from astrbot.api import logger

from ..config import PluginConfig
from ..constants import MOODS

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


class EmotionEngine:
    """Per-group emotion state machine."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter", plugin: Any = None) -> None:
        self._config = config.emotion
        self._llm = llm
        self._plugin = plugin
        self._prompts: Any = None  # lazy

    async def get_mood(self, group_id: str, db: "Database") -> str:
        """Get current mood for a group, applying natural decay if needed."""
        if not self._config.enabled:
            return "neutral"

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            state = await repo.get_or_create_emotion(
                group_id, self._config.initial_mood
            )

            # Natural decay: if mood hasn't been updated in decay_hours, reset
            if state.mood != "neutral" and self._config.decay_hours > 0:
                elapsed = _dt.datetime.now(_dt.timezone.utc) - (
                    state.updated_at.replace(tzinfo=_dt.timezone.utc)
                    if state.updated_at.tzinfo is None
                    else state.updated_at
                )
                if elapsed.total_seconds() > self._config.decay_hours * 3600:
                    await repo.update_emotion(group_id, "neutral", 0.0, 0.0)
                    await session.commit()
                    logger.debug(
                        f"[Emotion] Mood decayed to neutral for {group_id} "
                        f"(inactive {elapsed.total_seconds()/3600:.1f}h)"
                    )
                    return "neutral"

            await session.commit()
            return state.mood

    async def maybe_update(
        self,
        group_id: str,
        message: str,
        db: "Database",
    ) -> Optional[str]:
        """Possibly update mood based on message content.

        Uses sensitivity as probability gate → LLM emotion analysis → update DB.
        Returns new mood or None if no change.
        """
        if not self._config.enabled:
            return None

        # Probabilistic gate: only trigger update based on sensitivity
        if random.random() > self._config.sensitivity:
            return None

        # Skip very short messages
        if len(message.strip()) < 10:
            return None

        try:
            # LLM emotion analysis
            if self._prompts is None:
                self._prompts = getattr(self._plugin, "prompt_service", None)
            if not self._prompts:
                return None
            template = await self._prompts.get_prompt("EMOTION_ANALYZE")
            prompt = template.format(message=message[:1000])
            response = await self._llm.fast_chat(prompt)

            if not response:
                return None

            new_mood = self._parse_mood(response)
            if not new_mood:
                return None

            # Single session: check current mood + update if changed
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                state = await repo.get_or_create_emotion(
                    group_id, self._config.initial_mood
                )

                # Apply natural decay inline
                current = state.mood
                if current != "neutral" and self._config.decay_hours > 0:
                    elapsed = _dt.datetime.now(_dt.timezone.utc) - (
                        state.updated_at.replace(tzinfo=_dt.timezone.utc)
                        if state.updated_at.tzinfo is None
                        else state.updated_at
                    )
                    if elapsed.total_seconds() > self._config.decay_hours * 3600:
                        current = "neutral"

                if new_mood == current:
                    await session.commit()
                    return None

                await repo.update_emotion(
                    group_id,
                    new_mood,
                    valence=self._mood_to_valence(new_mood),
                    arousal=self._mood_to_arousal(new_mood),
                )
                await session.commit()

            logger.info(
                f"[Emotion] Mood updated: {current} → {new_mood} for {group_id}"
            )
            return new_mood

        except Exception as e:
            logger.debug(f"[Emotion] Update failed: {e}")
            return None

    async def set_mood(self, group_id: str, mood: str, db: "Database") -> None:
        """Manually set mood (admin command)."""
        if mood not in MOODS:
            logger.warning(f"[Emotion] Invalid mood '{mood}', valid: {MOODS}")
            return
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_emotion(group_id)
            await repo.update_emotion(
                group_id,
                mood,
                valence=self._mood_to_valence(mood),
                arousal=self._mood_to_arousal(mood),
            )
            await session.commit()
        logger.info(f"[Emotion] Manually set mood for {group_id} to {mood}")

    # ------------------------------------------------------------------ #
    #  Parsing & mapping helpers
    # ------------------------------------------------------------------ #

    def _parse_mood(self, response: str) -> Optional[str]:
        """Extract mood label from LLM response."""
        import re
        # Try exact match first (response is just the mood label)
        cleaned = response.strip().lower().rstrip(".,!;:。！，；：")
        if cleaned in MOODS:
            return cleaned
        # Fallback: check first word
        first_word = cleaned.split()[0] if cleaned.split() else ""
        if first_word in MOODS:
            return first_word
        # Last resort: check if any mood appears as a standalone word
        for mood in MOODS:
            if re.search(r'\b' + re.escape(mood) + r'\b', cleaned):
                return mood
        return None

    @staticmethod
    def _mood_to_valence(mood: str) -> float:
        """Map discrete mood to valence (-1 to 1)."""
        return {
            "happy": 0.7,
            "excited": 0.8,
            "neutral": 0.0,
            "bored": -0.2,
            "sad": -0.6,
            "angry": -0.8,
        }.get(mood, 0.0)

    @staticmethod
    def _mood_to_arousal(mood: str) -> float:
        """Map discrete mood to arousal (0 to 1)."""
        return {
            "happy": 0.5,
            "excited": 0.9,
            "neutral": 0.3,
            "bored": 0.1,
            "sad": 0.3,
            "angry": 0.8,
        }.get(mood, 0.3)
