"""
每群本地人格服务

为每个群组提供：
    - 绑定 AstrBot 系统中已注册的预设人格
    - 维护插件侧的本地基础人格
    - 基于群消息迭代生成可切换的人格版本
"""
from __future__ import annotations

import inspect
import re
from typing import Any as _Any, TYPE_CHECKING

from astrbot.api import logger

from ..config import PluginConfig
from ..prompts import templates as prompt_templates

Any = _Any  # noqa: A001

_PERSONA_SLOT_RE = re.compile(r'<qunyou_persona_slot>\s*(.*?)\s*</qunyou_persona_slot>', re.DOTALL)

if TYPE_CHECKING:
    from ..services.llm_adapter import LLMAdapter
    from ..db.engine import Database


class PersonaBindingService:
    """Manages per-group local personas and versioned learning."""

    def __init__(self, config: PluginConfig, llm: "LLMAdapter", context: object, plugin: Any = None) -> None:
        self._config = config.persona_binding
        self._global_config = config
        self._llm = llm
        self._context = context
        self._plugin = plugin
        self._prompts: Any = None  # lazy

    @staticmethod
    def _binding_has_persona_source(binding: Any) -> bool:
        if not binding:
            return False
        if getattr(binding, "active_version_id", None) is not None:
            return True
        if (getattr(binding, "base_persona_prompt", "") or "").strip():
            return True
        return bool(getattr(binding, "bound_persona_id", None))

    @staticmethod
    def has_persona_slot(system_prompt: str) -> bool:
        return bool(system_prompt and _PERSONA_SLOT_RE.search(system_prompt))

    @staticmethod
    def extract_persona_slot_content(system_prompt: str) -> str:
        prompt = (system_prompt or "").strip()
        if not prompt:
            return ""
        match = _PERSONA_SLOT_RE.search(prompt)
        if not match:
            return prompt
        return (match.group(1) or "").strip()

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _extract_persona_id(persona: Any) -> str | None:
        persona_id = (
            getattr(persona, "persona_id", None)
            or getattr(persona, "id", None)
            or getattr(persona, "name", None)
        )
        if persona_id is None:
            return None
        return str(persona_id).strip() or None

    @staticmethod
    def _extract_persona_display_name(persona: Any, persona_id: str) -> str:
        for attr in ("display_name", "nickname", "title", "name"):
            value = getattr(persona, attr, None)
            if value:
                text = str(value).strip()
                if text:
                    return text
        return persona_id

    @staticmethod
    def _extract_persona_prompt(persona: Any) -> str:
        prompt = getattr(persona, "prompt", None) or getattr(persona, "system_prompt", "")
        return str(prompt or "")

    def _get_persona_manager(self, context: object) -> Any:
        injector = getattr(context, "get_injector", None)
        if injector is None:
            return getattr(context, "persona_manager", None)

        inj = injector()
        persona_mgr = None
        get_fn = getattr(inj, "get", None)
        if get_fn:
            for module_name in (
                "astrbot.core.provider.personality",
                "astrbot.core.persona_mgr",
            ):
                try:
                    PersonaManager = __import__(module_name, fromlist=["PersonaManager"]).PersonaManager
                    persona_mgr = get_fn(PersonaManager)
                    if persona_mgr is not None:
                        break
                except (ImportError, AttributeError, Exception):
                    continue

        if persona_mgr is None:
            persona_mgr = getattr(context, "persona_manager", None)
        return persona_mgr

    async def _list_persona_objects(self, persona_mgr: Any) -> list[Any]:
        candidates: list[Any] = []

        personas = getattr(persona_mgr, "personas", None)
        if isinstance(personas, dict):
            candidates.extend(personas.values())
        elif isinstance(personas, list):
            candidates.extend(personas)

        get_all_personas = getattr(persona_mgr, "get_all_personas", None)
        if callable(get_all_personas):
            try:
                persona_list = await self._maybe_await(get_all_personas())
                candidates.extend(list(persona_list or []))
            except Exception:
                pass

        get_all = getattr(persona_mgr, "get_all", None)
        if callable(get_all):
            try:
                candidates.extend(list(get_all() or []))
            except Exception:
                pass

        deduped: list[Any] = []
        seen_ids: set[str] = set()
        for item in candidates:
            persona_id = self._extract_persona_id(item)
            if not persona_id or persona_id in seen_ids:
                continue
            seen_ids.add(persona_id)
            deduped.append(item)
        return deduped

    async def list_persona_catalog(self, context: object | None = None) -> list[dict[str, Any]]:
        target_context = context or self._context
        persona_mgr = self._get_persona_manager(target_context)
        if persona_mgr is None:
            logger.debug("[PersonaBinding] PersonaManager not available for catalog listing")
            return []

        catalog: list[dict[str, Any]] = []
        for persona in await self._list_persona_objects(persona_mgr):
            persona_id = self._extract_persona_id(persona)
            if not persona_id:
                continue
            raw_prompt = self._extract_persona_prompt(persona)
            effective_prompt = self.extract_persona_slot_content(raw_prompt)
            catalog.append(
                {
                    "persona_id": persona_id,
                    "display_name": self._extract_persona_display_name(persona, persona_id),
                    "prompt": raw_prompt,
                    "effective_prompt": effective_prompt,
                    "prompt_length": len(raw_prompt),
                    "effective_prompt_length": len(effective_prompt),
                    "has_persona_slot": self.has_persona_slot(raw_prompt),
                }
            )

        return sorted(catalog, key=lambda item: (item["display_name"].lower(), item["persona_id"].lower()))

    async def has_managed_persona(self, group_id: str, db: "Database") -> bool:
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)

        if not binding:
            return False
        if getattr(binding, "active_version_id", None) is not None:
            return True
        if (getattr(binding, "base_persona_prompt", "") or "").strip():
            return True

        bound_persona_id = getattr(binding, "bound_persona_id", None)
        if not bound_persona_id:
            return False

        live_persona = await self.get_persona_prompt_by_id(bound_persona_id, self._context)
        live_persona = self.extract_persona_slot_content(live_persona)
        return bool(live_persona)

    async def resolve_effective_persona_prompt(
        self,
        group_id: str,
        db: "Database",
    ) -> tuple[str, str]:
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding, active_text = await repo.get_persona_binding_with_active_persona(group_id)

        active_text = (active_text or "").strip()
        if active_text:
            return active_text, "active_version"

        base_persona_prompt = (getattr(binding, "base_persona_prompt", "") or "").strip()
        if base_persona_prompt:
            return base_persona_prompt, "base_persona"

        bound_persona_id = getattr(binding, "bound_persona_id", None) if binding else None
        if bound_persona_id:
            live_persona = await self.get_persona_prompt_by_id(bound_persona_id, self._context)
            live_persona = self.extract_persona_slot_content(live_persona)
            if live_persona:
                return live_persona, "astrbot_persona"

        return "", ""

    async def import_base_persona_from_astrbot(
        self,
        group_id: str,
        db: "Database",
        persona_id: str | None = None,
    ) -> tuple[bool, str | None, str]:
        target_persona_id = persona_id
        if not target_persona_id:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                binding = await repo.get_persona_binding(group_id)
                target_persona_id = binding.bound_persona_id if binding else None

        if not target_persona_id:
            return False, None, ""

        persona_prompt = await self.get_persona_prompt_by_id(target_persona_id, self._context)
        persona_prompt = self.extract_persona_slot_content(persona_prompt)
        if not persona_prompt:
            return False, target_persona_id, ""

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.update_persona_binding(
                group_id,
                bound_persona_id=target_persona_id,
                base_persona_prompt=persona_prompt,
            )
            await session.commit()

        return True, target_persona_id, persona_prompt

    async def inspect_bound_persona_slot(
        self,
        group_id: str,
        db: "Database",
        persona_id: str | None = None,
    ) -> dict[str, Any]:
        target_persona_id = persona_id
        if not target_persona_id:
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                binding = await repo.get_persona_binding(group_id)
                target_persona_id = binding.bound_persona_id if binding else None

        if not target_persona_id:
            return {
                "persona_id": None,
                "prompt_available": False,
                "has_persona_slot": False,
                "prompt_length": 0,
            }

        prompt = await self.get_persona_prompt_by_id(target_persona_id, self._context)
        prompt = prompt or ""
        return {
            "persona_id": target_persona_id,
            "prompt_available": bool(prompt),
            "has_persona_slot": self.has_persona_slot(prompt),
            "prompt_length": len(prompt),
        }

    async def increment_and_check(self, group_id: str, db: "Database") -> bool:
        """Increment persona learning message count; return True if threshold reached."""
        if not self._config.enabled or not self._config.auto_learning_enabled:
            return False

        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)

            binding = await repo.get_persona_binding(group_id)
            if not self._binding_has_persona_source(binding):
                await session.commit()
                return False
            if not binding.is_learning_enabled:
                await session.commit()
                return False

            count = await repo.increment_persona_message_count(group_id)
            await session.commit()
            return count >= self._global_config.group_persona.batch_learning_threshold

    async def run_combined_learning(self, group_id: str, db: "Database") -> None:
        """Learn a full group-local persona prompt and store it as a versioned replacement."""
        logger.info(f"[PersonaBinding] Group-local persona learning started for {group_id}")

        job_id = None
        try:
            # 1. Resolve current local/base persona state
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)

                binding, current_persona = await repo.get_persona_binding_with_active_persona(group_id)
                if binding is None or not (
                    self._binding_has_persona_source(binding)
                    or (current_persona or "").strip()
                ):
                    logger.info(
                        f"[PersonaBinding] Group {group_id} has no local/imported persona seed, skipping"
                    )
                    return

                job_id = await repo.create_learning_job(group_id, "combined_learn")
                await session.commit()

            # 2. Resolve base persona and current active persona
            base_persona = (getattr(binding, "base_persona_prompt", "") or "").strip()
            imported_persona = ""
            if not base_persona and binding and binding.bound_persona_id:
                imported_persona = await self.get_persona_prompt_by_id(
                    binding.bound_persona_id, self._context
                ) or ""
                imported_persona = self.extract_persona_slot_content(imported_persona)

            current_persona = (current_persona or "").strip()
            if not current_persona:
                current_persona = base_persona or imported_persona

            # 3. Fetch recent messages
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)
                messages = await repo.get_recent_messages(
                    group_id, limit=self._global_config.group_persona.batch_learning_threshold
                )

                min_required_messages = min(
                    20,
                    self._global_config.group_persona.batch_learning_threshold,
                )

                if len(messages) < min_required_messages:
                    logger.info(
                        f"[PersonaBinding] Not enough messages ({len(messages)}) "
                        f"for {group_id}, skipping"
                    )
                    await repo.complete_learning_job(
                        job_id,
                        {
                            "skipped": True,
                            "reason": "insufficient_messages",
                            "required_messages": min_required_messages,
                        },
                    )
                    await session.commit()
                    return

                msg_lines = []
                for m in reversed(messages):
                    name = m.sender_name or m.sender_id
                    msg_lines.append(f"[{name}]: {m.text}")
                messages_text = "\n".join(msg_lines[-100:])

            # 4. Build prompt and call LLM
            template = prompt_templates.COMBINED_LEARNING_PROMPT
            if self._prompts is None:
                self._prompts = getattr(self._plugin, "prompt_service", None)
            if self._prompts:
                template = (
                    await self._prompts.get_prompt("COMBINED_LEARNING_PROMPT")
                    or prompt_templates.COMBINED_LEARNING_PROMPT
                )
            prompt = template.format(
                base_persona=base_persona or imported_persona or "（未设置基础人格）",
                current_persona=current_persona or "（首次生成，请结合最近消息生成一版稳定人格）",
                original_persona=current_persona or base_persona or imported_persona or "（未设置基础人格）",
                messages=messages_text,
                count=len(messages),
            )

            new_combined = await self._llm.main_chat(prompt)

            if not new_combined or len(new_combined.strip()) < 10:
                logger.warning(
                    f"[PersonaBinding] LLM returned empty/short combined result for {group_id}"
                )
                async with db.session() as session:
                    from ..db.repo import Repository
                    repo = Repository(session)
                    await repo.fail_learning_job(job_id, "empty_result")
                    await session.commit()
                return

            # 5. Store as new persona version, reset counter, prune
            async with db.session() as session:
                from ..db.repo import Repository
                repo = Repository(session)

                review_enabled = self._global_config.review_gate.enabled_for_tone
                auto_activate = self._config.auto_apply_learned_tone and not review_enabled
                version = await repo.add_new_persona_version(
                    group_id,
                    persona_prompt=new_combined.strip(),
                    auto_activate=auto_activate,
                )

                review_id = None
                if review_enabled:
                    await repo.supersede_pending_reviews(group_id, "persona_version")
                    review = await repo.create_learned_prompt_review(
                        group_id=group_id,
                        prompt_type="persona_version",
                        old_value=current_persona,
                        proposed_value=new_combined.strip(),
                        change_summary="每群本地人格学习生成了新版本，等待管理员审核后激活。",
                        metadata_json={
                            "version_num": version.version_num,
                            "combined_length": len(new_combined),
                            "messages_analyzed": len(messages),
                        },
                        target_persona_version_id=version.id,
                    )
                    review_id = review.id

                await repo.reset_persona_message_count(group_id)

                pruned = await repo.prune_old_persona_versions(
                    group_id, keep_count=self._config.max_tone_history_versions
                )

                await repo.complete_learning_job(
                    job_id,
                    {
                        "version_num": version.version_num,
                        "combined_length": len(new_combined),
                        "messages_analyzed": len(messages),
                        "auto_activated": auto_activate,
                        "review_required": review_enabled,
                        "review_id": review_id,
                        "pruned_versions": pruned,
                    },
                )
                await session.commit()

            if review_enabled:
                logger.info(
                    f"[PersonaBinding] Group-local persona pending review for {group_id}, "
                    f"version={version.version_num}"
                )
            else:
                logger.info(
                    f"[PersonaBinding] Group-local persona updated for {group_id}, "
                    f"version={version.version_num}, auto_activated={auto_activate}"
                )

        except Exception as e:
            logger.error(
                f"[PersonaBinding] Combined learning failed for {group_id}: {e}",
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
            persona_mgr = self._get_persona_manager(context)

            if persona_mgr is None:
                logger.debug("[PersonaBinding] PersonaManager not available")
                return None

            # Get the persona by ID
            persona = None

            get_persona = getattr(persona_mgr, "get_persona", None)
            if get_persona is not None:
                try:
                    persona = await self._maybe_await(get_persona(persona_id))
                except Exception:
                    persona = None

            if persona is None:
                for item in await self._list_persona_objects(persona_mgr):
                    if self._extract_persona_id(item) == persona_id:
                        persona = item
                        break

            if persona is None:
                logger.warning(
                    f"[PersonaBinding] Persona '{persona_id}' "
                    f"not found in PersonaManager"
                )
                return None

            # Extract system_prompt
            prompt = self._extract_persona_prompt(persona)
            return prompt if prompt else None

        except Exception as e:
            logger.debug(f"[PersonaBinding] Failed to get persona prompt: {e}")
            return None
