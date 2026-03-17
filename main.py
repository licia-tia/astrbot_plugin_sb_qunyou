"""
astrbot_plugin_sb_qunyou — 群聊智能体插件

主插件类，负责：
  - 消息监听 (on_message) — 事件驱动防抖
  - 防抖 → 存储 → 话题路由 → 后台学习触发
  - LLM 请求注入 (on_llm_request)
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter
from astrbot.api.event.filter import PermissionType
import astrbot.api.star as star
from astrbot.api.star import Context
from astrbot.api import logger, AstrBotConfig

from .config import PluginConfig
from .constants import INGESTION_BUFFER_GLOBAL_MAX
from .lifecycle import Lifecycle


class QunyouPlugin(star.Star):
    """群聊智能体 · 千群千面"""

    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.context = context

        # Pre-initialize all attributes to avoid AttributeError
        self.db = None
        self.llm = None
        self.group_persona = None
        self.speaker_memory = None
        self.emotion = None
        self.jargon = None
        self.hook_handler = None
        self.debounce = None
        self.topic_router = None
        self.knowledge = None      # LightRAGKnowledgeManager (lazy-init)
        self.reranker = None       # IRerankProvider (lazy-init)
        self.persona_binding = None  # PersonaBindingService
        self.background_tasks: set[asyncio.Task] = set()
        self._ingestion_buffer: dict[str, list[str]] = {}  # knowledge ingestion
        self._ingestion_tasks: dict[str, asyncio.Task] = {}
        self._tone_learning_locks: set[str] = set()  # guard against concurrent learning

        # Load config
        raw = config if isinstance(config, dict) else (config or {})
        try:
            self.plugin_config = PluginConfig.from_astrbot_config(raw)
        except Exception as e:
            logger.warning(f"[Qunyou] Config parse error, using defaults: {e}")
            self.plugin_config = PluginConfig()

        # Lifecycle orchestration
        self._lifecycle = Lifecycle(self)
        try:
            self._lifecycle.bootstrap(self.plugin_config, context)
        except Exception as e:
            logger.error(f"[Qunyou] Bootstrap failed: {e}", exc_info=True)

        logger.info("[Qunyou] Plugin initialized")

    # ================================================================== #
    #  Lifecycle
    # ================================================================== #

    async def initialize(self):
        """Called by AstrBot after handler binding."""
        await self._lifecycle.on_load()

    async def terminate(self):
        """Called on plugin unload/disable."""
        # Flush debounce buffer
        if self.debounce:
            await self.debounce.flush_all()
        if self._ingestion_tasks:
            await asyncio.gather(*list(self._ingestion_tasks.values()), return_exceptions=True)
        # Flush jargon counters
        if self.jargon and self.db:
            try:
                await self.jargon.flush_all(self.db)
            except Exception:
                pass
        # Flush knowledge ingestion buffer
        if self._ingestion_buffer and self.knowledge:
            for gid, texts in self._ingestion_buffer.items():
                for txt in texts:
                    try:
                        await self.knowledge.insert(gid, txt)
                    except Exception:
                        pass
            self._ingestion_buffer.clear()
        await self._lifecycle.shutdown()

    # ================================================================== #
    #  Message listener — event-driven debounce
    # ================================================================== #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """Listen to all messages → debounce (event-driven) → process.

        Event-driven debounce pattern (from continuous_message):
          Msg1 → create session, block (await flush_event.wait())
          Msg2 → append, event.stop_event(), reset timer
          Timer → flush_event.set() → Msg1 handler wakes up
          → event rewritten with merged text → continues to LLM
        """
        if not self.db or not self.db.engine:
            return

        if self.debounce and self.plugin_config.debounce.mode != "off":
            # Event-driven debounce: may block on first msg, stop subsequent
            result = await self.debounce.handle_event(event)
            if not result:
                return  # event was intercepted or empty

            # After debounce settlement, event.message_str is updated
            # with merged text. Continue to process.
            text = event.get_message_str()
        else:
            text = event.get_message_str()

        if not text or not text.strip():
            return

        group_id = event.get_group_id() or event.get_sender_id()
        sender_id = event.get_sender_id()

        # Fire background processing (non-blocking)
        self._fire_and_forget(
            self._process_message(group_id, sender_id, text, event)
        )

    async def _process_message(
        self,
        group_id: str,
        sender_id: str,
        text: str,
        event: Optional[AstrMessageEvent] = None,
    ) -> None:
        """Core message processing: store → route → background tasks."""
        if not self.db:
            return

        try:
            sender_name = ""
            platform = ""
            if event:
                sender_name = event.get_sender_name() or ""
                platform = getattr(event, "platform_name", "") or ""

            # 1. Save raw message
            async with self.db.session() as session:
                from .db.repo import Repository
                repo = Repository(session)
                msg_id = await repo.save_raw_message(
                    group_id=group_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text_content=text,
                    platform=platform,
                )
                await session.commit()

            # 2. Topic routing (async, non-blocking)
            if self.topic_router:
                self._fire_and_forget(
                    self.topic_router.route_message(group_id, msg_id, text, self.db)
                )

            # 3. Jargon word counting (in-memory, fast) + periodic flush
            if self.jargon:
                total = self.jargon.count_words(group_id, text)
                if total >= self.plugin_config.jargon.flush_threshold:
                    self._fire_and_forget(
                        self.jargon.flush_to_db(group_id, self.db)
                    )

            # 4. Group persona: check if batch threshold reached
            if self.group_persona:
                self._fire_and_forget(
                    self._check_persona_learning(group_id)
                )

            # 5. Speaker memory extraction (async)
            if self.speaker_memory:
                self._fire_and_forget(
                    self.speaker_memory.extract_and_store(
                        group_id, sender_id, sender_name, text, self.db
                    )
                )

            # 6. Emotion update (async, probabilistic)
            if self.emotion and event and event.is_at_or_wake_command:
                self._fire_and_forget(
                    self.emotion.maybe_update(group_id, text, self.db)
                )

            # 7. Persona binding: tone learning message counter
            if self.persona_binding and self.plugin_config.persona_binding.enabled:
                self._fire_and_forget(
                    self._check_tone_learning(group_id)
                )

            # 8. Knowledge ingestion buffer (LightRAG)
            if (
                self.knowledge
                and len(text.strip()) >= self.plugin_config.knowledge.min_ingestion_length
            ):
                # Global cap: prevent unbounded memory growth
                total_buffered = sum(len(v) for v in self._ingestion_buffer.values())
                if total_buffered >= INGESTION_BUFFER_GLOBAL_MAX:
                    # Force flush oldest group's buffer
                    if self._ingestion_buffer:
                        oldest_gid = next(iter(self._ingestion_buffer))
                        self._schedule_knowledge_flush(oldest_gid)

                buf = self._ingestion_buffer.setdefault(group_id, [])
                buf.append(text)
                if len(buf) >= self.plugin_config.knowledge.ingestion_buffer_max:
                    self._schedule_knowledge_flush(group_id)

        except Exception as e:
            logger.error(f"[Qunyou] Message processing error: {e}", exc_info=True)

    async def _check_persona_learning(self, group_id: str) -> None:
        """Check if batch learning threshold is reached."""
        if self.group_persona and self.db:
            should_learn = await self.group_persona.increment_and_check(
                group_id, self.db
            )
            if should_learn:
                self._fire_and_forget(
                    self.group_persona.run_batch_learning(group_id, self.db)
                )

    async def _check_tone_learning(self, group_id: str) -> None:
        """Check if tone learning threshold is reached for persona binding."""
        if self.persona_binding and self.db:
            if group_id in self._tone_learning_locks:
                return  # already learning for this group
            self._tone_learning_locks.add(group_id)  # Claim BEFORE await
            try:
                should_learn = await self.persona_binding.increment_and_check(
                    group_id, self.db
                )
                if should_learn:
                    await self.persona_binding.run_tone_learning(group_id, self.db)
            finally:
                self._tone_learning_locks.discard(group_id)

    # ================================================================== #
    #  LLM Hook
    # ================================================================== #

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req=None):
        """Inject context into LLM requests."""
        if self.hook_handler:
            await self.hook_handler.handle(event, req)

    # ================================================================== #
    #  Commands
    # ================================================================== #

    @filter.command("qunyou_status")
    @filter.permission_type(PermissionType.ADMIN)
    async def status_command(self, event: AstrMessageEvent):
        """查看群聊智能体状态"""
        group_id = event.get_group_id() or event.get_sender_id()
        parts = [f"🤖 群聊智能体状态 [{group_id}]"]

        if self.db:
            parts.append("✅ 数据库已连接")
        else:
            parts.append("❌ 数据库未连接")

        if self.emotion and self.db:
            try:
                mood = await self.emotion.get_mood(group_id, self.db)
                parts.append(f"😊 当前情绪: {mood}")
            except Exception:
                parts.append("😊 情绪: 获取失败")

        mode = self.plugin_config.debounce.mode
        parts.append(f"🔧 防抖模式: {mode}")
        if mode != "off":
            parts.append(f"   ⏱ 窗口: {self.plugin_config.debounce.time_window_seconds}s")
            parts.append(f"   ✍ 输入感知: {'开' if self.plugin_config.debounce.enable_typing_detection else '关'}")
        parts.append(f"📋 话题路由: {'开' if self.plugin_config.topic.enabled else '关'}")
        parts.append(f"📝 黑话统计: {'开' if self.plugin_config.jargon.enabled else '关'}")
        parts.append(
            f"📚 知识引擎: {self.plugin_config.knowledge.engine}"
            f" ({'ready' if self.knowledge else 'off'})"
        )
        parts.append(
            f"🔀 Reranker: {'开' if self.reranker else '关'}"
        )

        # Cache stats
        try:
            from .utils.cache import get_cache_manager
            cm = get_cache_manager()
            stats = cm.get_stats()
            if stats:
                hit_info = ", ".join(
                    f"{k}:{v['hit_rate']:.0%}" for k, v in stats.items()
                )
                parts.append(f"💾 缓存命中率: {hit_info}")
        except Exception:
            pass

        yield event.plain_result("\n".join(parts))

    @filter.command("set_mood")
    @filter.permission_type(PermissionType.ADMIN)
    async def set_mood_command(self, event: AstrMessageEvent):
        """设置情绪: /set_mood happy"""
        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        mood = parts[0] if parts else ""

        if not mood:
            yield event.plain_result("用法: /set_mood happy|neutral|angry|sad|excited|bored")
            return

        if self.emotion and self.db:
            group_id = event.get_group_id() or event.get_sender_id()
            await self.emotion.set_mood(group_id, mood, self.db)
            yield event.plain_result(f"情绪已设置为: {mood}")
        else:
            yield event.plain_result("情绪引擎未启用")

    # ================================================================== #
    #  Persona Binding Commands
    # ================================================================== #

    @filter.command("qunyou_bind")
    @filter.permission_type(PermissionType.ADMIN)
    async def bind_persona_command(self, event: AstrMessageEvent):
        """绑定预设人格: /qunyou_bind <persona_id>"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        persona_id = parts[0] if parts else ""

        if not persona_id:
            yield event.plain_result("用法: /qunyou_bind <persona_id>")
            return

        # Validate persona exists (best-effort, non-blocking if PersonaManager unavailable)
        if self.persona_binding:
            prompt = await self.persona_binding.get_persona_prompt_by_id(persona_id, self.context)
            if prompt is None:
                yield event.plain_result(
                    f"警告: 未找到人格 '{persona_id}'，可能尚未注册或 PersonaManager 不可用。\n"
                    f"仍将保存绑定，请确认人格 ID 正确。"
                )

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, bound_persona_id=persona_id)
            await session.commit()

        yield event.plain_result(f"已绑定预设人格: {persona_id}")

    @filter.command("qunyou_unbind")
    @filter.permission_type(PermissionType.ADMIN)
    async def unbind_persona_command(self, event: AstrMessageEvent):
        """解除人格绑定: /qunyou_unbind"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, bound_persona_id=None)
            await session.commit()

        yield event.plain_result("已解除人格绑定")

    @filter.command("qunyou_tone_learn")
    @filter.permission_type(PermissionType.ADMIN)
    async def tone_learn_command(self, event: AstrMessageEvent):
        """强制触发语气学习: /qunyou_tone_learn"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        yield event.plain_result("正在触发语气学习，请稍候...")
        try:
            await self.persona_binding.run_tone_learning(group_id, self.db)
            yield event.plain_result("语气学习完成！使用 /qunyou_tone_status 查看结果。")
        except Exception as e:
            yield event.plain_result(f"语气学习失败: {e}")

    @filter.command("qunyou_tone_status")
    @filter.permission_type(PermissionType.ADMIN)
    async def tone_status_command(self, event: AstrMessageEvent):
        """查看语气绑定状态: /qunyou_tone_status"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            binding, active_tone = await repo.get_persona_binding_with_active_tone(group_id)

            if not binding:
                yield event.plain_result("当前群尚未配置人格绑定")
                return

            parts = [f"🎭 人格绑定状态 [{group_id}]"]
            parts.append(f"绑定人格: {binding.bound_persona_id or '未绑定'}")
            parts.append(f"语气学习: {'开' if binding.is_learning_enabled else '关'}")
            parts.append(f"消息累积: {binding.tone_message_count}")

            if binding.active_version_id:
                parts.append(f"\n📝 当前激活语气版本:")
                parts.append(active_tone[:200] + "..." if len(active_tone) > 200 else active_tone)

            versions = await repo.get_tone_versions(group_id)
            if versions:
                parts.append(f"\n📚 历史版本 ({len(versions)} 个):")
                for v in versions[:5]:
                    active_mark = " ✅" if v.id == binding.active_version_id else ""
                    manual_mark = " [手动]" if v.is_manual else ""
                    parts.append(
                        f"  V{v.version_num}{active_mark}{manual_mark} "
                        f"- {v.created_at.strftime('%m-%d %H:%M')}"
                    )
                if len(versions) > 5:
                    parts.append(f"  ... 还有 {len(versions) - 5} 个更早版本")

        yield event.plain_result("\n".join(parts))

    @filter.command("qunyou_tone_switch")
    @filter.permission_type(PermissionType.ADMIN)
    async def tone_switch_command(self, event: AstrMessageEvent):
        """切换语气版本: /qunyou_tone_switch <version_num>"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        try:
            version_num = int(parts[0]) if parts else 0
        except ValueError:
            yield event.plain_result("用法: /qunyou_tone_switch <版本号>")
            return

        if version_num <= 0:
            yield event.plain_result("用法: /qunyou_tone_switch <版本号>")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            success = await repo.set_active_tone_version_by_num(group_id, version_num)
            await session.commit()

        if success:
            yield event.plain_result(f"已切换到语气版本 V{version_num}")
        else:
            yield event.plain_result(f"版本 V{version_num} 不存在")

    @filter.command("qunyou_tone_toggle")
    @filter.permission_type(PermissionType.ADMIN)
    async def tone_toggle_command(self, event: AstrMessageEvent):
        """开关语气学习: /qunyou_tone_toggle true/false"""
        if not self.persona_binding or not self.db:
            yield event.plain_result("人格绑定功能未启用")
            return

        text = event.message_str.strip().lower()
        if text in ("true", "on", "1", "开"):
            enabled = True
        elif text in ("false", "off", "0", "关"):
            enabled = False
        else:
            yield event.plain_result("用法: /qunyou_tone_toggle true/false")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, is_learning_enabled=enabled)
            await session.commit()

        yield event.plain_result(f"语气学习已{'开启' if enabled else '关闭'}")

    @filter.command("qunyou_review_pending")
    @filter.permission_type(PermissionType.ADMIN)
    async def review_pending_command(self, event: AstrMessageEvent):
        """查看当前群待审核学习记录"""
        if not self.db:
            yield event.plain_result("数据库未连接")
            return

        group_id = event.get_group_id() or event.get_sender_id()
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            reviews = await repo.get_pending_learned_prompt_reviews(group_id=group_id, limit=10)

        if not reviews:
            yield event.plain_result("当前群暂无待审核学习记录")
            return

        parts = [f"🧾 待审核学习记录 [{group_id}]"]
        for review in reviews:
            parts.append(
                f"#{review.id} {review.prompt_type} - {review.created_at.strftime('%m-%d %H:%M')}"
            )
            if review.change_summary:
                parts.append(f"  {review.change_summary[:120]}")
        yield event.plain_result("\n".join(parts))

    @filter.command("qunyou_review_approve")
    @filter.permission_type(PermissionType.ADMIN)
    async def review_approve_command(self, event: AstrMessageEvent):
        """批准学习记录: /qunyou_review_approve <review_id> [notes]"""
        if not self.db:
            yield event.plain_result("数据库未连接")
            return

        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        try:
            review_id = int(parts[0]) if parts else 0
        except ValueError:
            yield event.plain_result("用法: /qunyou_review_approve <review_id> [notes]")
            return

        if review_id <= 0:
            yield event.plain_result("用法: /qunyou_review_approve <review_id> [notes]")
            return

        notes = parts[1] if len(parts) > 1 else ""
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            ok = await repo.approve_learned_prompt_review(
                review_id,
                reviewed_by=event.get_sender_id(),
                review_notes=notes,
                max_group_history_versions=self.plugin_config.group_persona.max_history_versions,
            )
            await session.commit()

        if ok:
            yield event.plain_result(f"已批准学习记录 #{review_id}")
        else:
            yield event.plain_result(f"学习记录 #{review_id} 不存在、已处理，或无法批准")

    @filter.command("qunyou_review_reject")
    @filter.permission_type(PermissionType.ADMIN)
    async def review_reject_command(self, event: AstrMessageEvent):
        """拒绝学习记录: /qunyou_review_reject <review_id> [notes]"""
        if not self.db:
            yield event.plain_result("数据库未连接")
            return

        text = event.message_str.strip()
        parts = text.split(maxsplit=1)
        try:
            review_id = int(parts[0]) if parts else 0
        except ValueError:
            yield event.plain_result("用法: /qunyou_review_reject <review_id> [notes]")
            return

        if review_id <= 0:
            yield event.plain_result("用法: /qunyou_review_reject <review_id> [notes]")
            return

        notes = parts[1] if len(parts) > 1 else ""
        async with self.db.session() as session:
            from .db.repo import Repository
            repo = Repository(session)
            ok = await repo.reject_learned_prompt_review(
                review_id,
                reviewed_by=event.get_sender_id(),
                review_notes=notes,
            )
            await session.commit()

        if ok:
            yield event.plain_result(f"已拒绝学习记录 #{review_id}")
        else:
            yield event.plain_result(f"学习记录 #{review_id} 不存在、已处理，或无法拒绝")

    # ================================================================== #
    #  Utility
    # ================================================================== #

    def _fire_and_forget(self, coro) -> None:
        """Launch a background task with proper tracking and error logging."""
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Callback for background tasks: discard from set and log errors."""
        self.background_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error(
                f"[Qunyou] Background task failed: {task.exception()}",
                exc_info=task.exception(),
            )

    def _schedule_knowledge_flush(self, group_id: str) -> None:
        if group_id in self._ingestion_tasks:
            return
        task = asyncio.create_task(self._flush_knowledge_buffer(group_id))
        self._ingestion_tasks[group_id] = task
        self.background_tasks.add(task)

        def _on_done(done_task: asyncio.Task, gid: str = group_id) -> None:
            self._ingestion_tasks.pop(gid, None)
            self._on_background_task_done(done_task)

        task.add_done_callback(_on_done)

    async def _flush_knowledge_buffer(self, group_id: str) -> None:
        """Flush buffered messages for a group through LightRAG."""
        texts = self._ingestion_buffer.pop(group_id, [])
        if not texts or not self.knowledge:
            return
        combined = "\n\n".join(texts)
        try:
            await self.knowledge.insert(group_id, combined)
            logger.debug(
                f"[Qunyou] Knowledge ingestion: {group_id}, "
                f"{len(texts)} messages"
            )
        except Exception as e:
            logger.debug(f"[Qunyou] Knowledge ingestion failed: {e}")
