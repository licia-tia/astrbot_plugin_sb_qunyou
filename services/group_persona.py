"""
千群千面 — 群画像服务

为每个 group_id 维护独立的"群画像"：
  - base_prompt: 人类预设的客观描述（如"原神讨论群"）
  - source_whitelist: 可信搜索信源 URL
  - learned_prompt: 后台 batch 学习自动更新的群画像

异步学习流程：
  消息累积 >= batch_threshold → 取最近 N 条 → LLM 总结 → 更新 learned_prompt
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any as _Any, TYPE_CHECKING

Any = _Any  # noqa: A001

from astrbot.api import logger

from ..config import PluginConfig

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database
    from ..db.repo import Repository


class GroupPersonaService:
    """Manages per-group persona profiles."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter", plugin: Any = None) -> None:
        self._config = config.group_persona
        self._global_config = config
        self._llm = llm
        self._plugin = plugin
        self._prompts: Any = None  # lazy

    async def get_group_prompt(self, group_id: str, db: "Database") -> str:
        """Return the combined prompt for a group (base + learned)."""
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            profile = await repo.get_or_create_group_profile(group_id)
            await session.commit()

            parts = []
            if profile.base_prompt:
                parts.append(profile.base_prompt)
            if profile.learned_prompt:
                parts.append(profile.learned_prompt)
            return "\n".join(parts) if parts else ""

    async def increment_and_check(self, group_id: str, db: "Database") -> bool:
        """Increment message count; return True if batch threshold reached."""
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            count = await repo.increment_group_message_count(group_id)
            await session.commit()
            return count >= self._config.batch_learning_threshold

    async def run_batch_learning(self, group_id: str, db: "Database") -> None:
        """Run batch learning: summarize recent messages and update group persona.

        Steps:
          1. Fetch recent N messages
          2. Format as conversation log
          3. LLM summarize → extract group characteristics
          4. Update learned_prompt (with history)
          5. Reset message counter
        """
        logger.info(f"[GroupPersona] Batch learning started for {group_id}")

        job_id = None
        try:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)

                # 1. Create learning job record
                job_id = await repo.create_learning_job(group_id, "persona_learn")
                await session.commit()

            # 2. Fetch recent messages
            async with db.session() as session:
                repo = Repository(session)
                messages = await repo.get_recent_messages(
                    group_id, limit=self._config.batch_learning_threshold
                )

                if len(messages) < 20:
                    logger.info(
                        f"[GroupPersona] Not enough messages ({len(messages)}) "
                        f"for {group_id}, skipping"
                    )
                    await repo.complete_learning_job(
                        job_id, {"skipped": True, "reason": "insufficient_messages"}
                    )
                    await session.commit()
                    return

                # 3. Format messages for LLM
                msg_lines = []
                for m in reversed(messages):  # chronological
                    name = m.sender_name or m.sender_id
                    msg_lines.append(f"[{name}]: {m.text}")

                messages_text = "\n".join(msg_lines[-100:])  # cap at 100 lines

            # 4. LLM summarize
            if self._prompts is None:
                self._prompts = getattr(self._plugin, "prompt_service", None)
            if not self._prompts:
                try:
                    async with db.session() as session:
                        from ..db.repo import Repository
                        repo = Repository(session)
                        await repo.fail_learning_job(job_id, "prompt_service_unavailable")
                        await session.commit()
                except Exception:
                    pass
                return
            template = await self._prompts.get_prompt("GROUP_PERSONA_LEARN")
            prompt = template.format(messages=messages_text)
            summary = await self._llm.main_chat(prompt)

            if not summary or len(summary.strip()) < 10:
                logger.warning(
                    f"[GroupPersona] LLM returned empty/short summary for {group_id}"
                )
                async with db.session() as session:
                    repo = Repository(session)
                    await repo.fail_learning_job(job_id, "empty_summary")
                    await session.commit()
                return

            # 5. Update profile or create pending review
            async with db.session() as session:
                repo = Repository(session)
                profile = await repo.get_or_create_group_profile(group_id)

                # Build history
                history = profile.learned_prompt_history or []
                if profile.learned_prompt:
                    history.append({
                        "prompt": profile.learned_prompt,
                        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    })
                    # Keep only last N versions
                    history = history[-self._config.max_history_versions:]

                review_enabled = self._global_config.review_gate.enabled_for_group_persona
                review_id = None
                if review_enabled:
                    await repo.supersede_pending_reviews(group_id, "group_persona")
                    review = await repo.create_learned_prompt_review(
                        group_id=group_id,
                        prompt_type="group_persona",
                        old_value=profile.learned_prompt,
                        proposed_value=summary.strip(),
                        change_summary="群画像学习生成了新的 learned_prompt，等待管理员审核后生效。",
                        metadata_json={
                            "messages_analyzed": len(messages),
                            "summary_length": len(summary),
                        },
                    )
                    review_id = review.id
                    await repo.update_group_profile(
                        group_id,
                        message_count_since_learn=0,
                    )
                else:
                    await repo.update_group_profile(
                        group_id,
                        learned_prompt=summary.strip(),
                        learned_prompt_history=history,
                        message_count_since_learn=0,
                    )
                await repo.complete_learning_job(
                    job_id,
                    {
                        "summary_length": len(summary),
                        "messages_analyzed": len(messages),
                        "review_required": review_enabled,
                        "review_id": review_id,
                    },
                )
                await session.commit()

            if review_enabled:
                logger.info(
                    f"[GroupPersona] Batch learning created pending review for {group_id}, "
                    f"summary: {len(summary)} chars"
                )
            else:
                logger.info(
                    f"[GroupPersona] Batch learning completed for {group_id}, "
                    f"summary: {len(summary)} chars"
                )

        except Exception as e:
            logger.error(
                f"[GroupPersona] Batch learning failed for {group_id}: {e}",
                exc_info=True,
            )
            if job_id is not None:
                try:
                    async with db.session() as session:
                        repo = Repository(session)
                        await repo.fail_learning_job(job_id, str(e))
                        await session.commit()
                except Exception:
                    pass
