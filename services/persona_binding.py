"""
独立人格绑定与语气学习服务

为每个群组提供：
  - 绑定 AstrBot 系统中已注册的预设人格
  - 独立于群画像（group_persona）的语气学习流程
  - 语气版本控制（Versioning）
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

from ..config import PluginConfig
from ..prompts.templates import TONE_LEARNING_PROMPT

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


class PersonaBindingService:
    """Manages per-group persona binding and independent tone learning."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter") -> None:
        self._config = config.persona_binding
        self._global_config = config
        self._llm = llm

    async def increment_and_check(self, group_id: str, db: "Database") -> bool:
        """Increment tone message count; return True if threshold reached."""
        if not self._config.enabled or not self._config.auto_learning_enabled:
            return False

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)

            binding = await repo.get_or_create_persona_binding(group_id)
            if not binding.is_learning_enabled:
                await session.commit()
                return False

            count = await repo.increment_tone_message_count(group_id)
            await session.commit()
            return count >= self._config.tone_learning_threshold

    async def run_tone_learning(self, group_id: str, db: "Database") -> None:
        """Run tone learning for a group.

        Steps:
          1. Fetch current active tone (if any)
          2. Fetch recent messages
          3. Build prompt with current tone + messages
          4. Send to LLM for iterative tone extraction
          5. Store new version in PersonaToneVersion
          6. Optionally auto-activate based on config
          7. Reset message counter
          8. Prune old versions
        """
        logger.info(f"[PersonaBinding] Tone learning started for {group_id}")

        job_id = None
        try:
            # 1. Get current active tone
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)

                binding, current_tone = await repo.get_persona_binding_with_active_tone(group_id)
                if binding is None:
                    # Ensure binding exists
                    binding = await repo.get_or_create_persona_binding(group_id)
                    current_tone = ""
                    await session.commit()

                # Create learning job record
                job_id = await repo.create_learning_job(group_id, "tone_learn")
                await session.commit()

            # 2. Fetch recent messages
            async with db.session() as session:
                repo = Repository(session)
                messages = await repo.get_recent_messages(
                    group_id, limit=self._config.tone_learning_threshold
                )

                if len(messages) < 20:
                    logger.info(
                        f"[PersonaBinding] Not enough messages ({len(messages)}) "
                        f"for {group_id}, skipping"
                    )
                    await repo.complete_learning_job(
                        job_id, {"skipped": True, "reason": "insufficient_messages"}
                    )
                    await session.commit()
                    return

                # Format messages
                msg_lines = []
                for m in reversed(messages):  # chronological
                    name = m.sender_name or m.sender_id
                    msg_lines.append(f"[{name}]: {m.text}")
                messages_text = "\n".join(msg_lines[-100:])  # cap at 100

            # 3. Build prompt
            if current_tone:
                current_tone_section = (
                    f"当前已有的语气描述（请在此基础上迭代演化）：\n{current_tone}"
                )
            else:
                current_tone_section = "（这是首次学习，请从零开始总结语气特征）"

            prompt = TONE_LEARNING_PROMPT.format(
                current_tone_section=current_tone_section,
                messages=messages_text,
            )

            # 4. LLM summarize
            new_tone = await self._llm.main_chat(prompt)

            if not new_tone or len(new_tone.strip()) < 10:
                logger.warning(
                    f"[PersonaBinding] LLM returned empty/short tone for {group_id}"
                )
                async with db.session() as session:
                    repo = Repository(session)
                    await repo.fail_learning_job(job_id, "empty_tone")
                    await session.commit()
                return

            # 5-7. Store new version, reset counter, prune
            async with db.session() as session:
                repo = Repository(session)

                review_enabled = self._global_config.review_gate.enabled_for_tone
                auto_activate = self._config.auto_apply_learned_tone and not review_enabled
                version = await repo.add_new_tone_version(
                    group_id,
                    learned_tone=new_tone.strip(),
                    auto_activate=auto_activate,
                )

                review_id = None
                if review_enabled:
                    await repo.supersede_pending_reviews(group_id, "tone_version")
                    review = await repo.create_learned_prompt_review(
                        group_id=group_id,
                        prompt_type="tone_version",
                        old_value=current_tone,
                        proposed_value=new_tone.strip(),
                        change_summary="语气学习生成了新的版本，等待管理员审核后激活。",
                        metadata_json={
                            "version_num": version.version_num,
                            "tone_length": len(new_tone),
                        },
                        target_tone_version_id=version.id,
                    )
                    review_id = review.id

                await repo.reset_tone_message_count(group_id)

                # Prune old versions
                pruned = await repo.prune_old_tone_versions(
                    group_id, keep_count=self._config.max_tone_history_versions
                )

                await repo.complete_learning_job(
                    job_id,
                    {
                        "version_num": version.version_num,
                        "tone_length": len(new_tone),
                        "auto_activated": auto_activate,
                        "review_required": review_enabled,
                        "review_id": review_id,
                        "pruned_versions": pruned,
                    },
                )
                await session.commit()

            if review_enabled:
                logger.info(
                    f"[PersonaBinding] Tone learning created pending review for {group_id}, "
                    f"version={version.version_num}"
                )
            else:
                logger.info(
                    f"[PersonaBinding] Tone learning completed for {group_id}, "
                    f"version={version.version_num}, auto_activated={auto_activate}"
                )

        except Exception as e:
            logger.error(
                f"[PersonaBinding] Tone learning failed for {group_id}: {e}",
                exc_info=True,
            )
            if job_id is not None:
                try:
                    async with db.session() as session:
                        from ..db.repo import Repository
                        repo = Repository(session)
                        await repo.fail_learning_job(job_id, str(e))
                        await session.commit()
                except Exception:
                    pass

    async def get_persona_prompt_by_id(
        self, persona_id: str, context: object
    ) -> str | None:
        """Retrieve the system_prompt of a persona from AstrBot PersonaManager.

        Returns None if PersonaManager unavailable or persona not found.
        """
        try:
            injector = getattr(context, "get_injector", None)
            if injector is None:
                return None
            inj = injector()

            # Try to get PersonaManager
            persona_mgr = None
            get_fn = getattr(inj, "get", None)
            if get_fn:
                try:
                    from astrbot.core.provider.personality import PersonaManager
                    persona_mgr = get_fn(PersonaManager)
                except (ImportError, Exception):
                    pass

            if persona_mgr is None:
                # Fallback: try accessing via context attributes
                persona_mgr = getattr(context, "persona_manager", None)

            if persona_mgr is None:
                logger.debug("[PersonaBinding] PersonaManager not available")
                return None

            # Get the persona by ID
            personas = getattr(persona_mgr, "personas", {})
            persona = personas.get(persona_id)
            if persona is None:
                # Try list-based lookup
                persona_list = getattr(persona_mgr, "get_all", None)
                if persona_list:
                    for p in persona_list():
                        pid = getattr(p, "id", None) or getattr(p, "name", None)
                        if pid == persona_id:
                            persona = p
                            break

            if persona is None:
                logger.warning(
                    f"[PersonaBinding] Persona '{persona_id}' "
                    f"not found in PersonaManager"
                )
                return None

            # Extract system_prompt
            prompt = getattr(persona, "prompt", None) or getattr(persona, "system_prompt", "")
            return prompt if prompt else None

        except Exception as e:
            logger.debug(f"[PersonaBinding] Failed to get persona prompt: {e}")
            return None
