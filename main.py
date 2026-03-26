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
        self._tone_learning_locks: set[str] = set()  # guard against concurrent persona learning
        self._prompt_slot_status: dict[str, dict[str, Any]] = {}

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
            for task in list(self._ingestion_tasks.values()):
                task.cancel()
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

            # 7. Per-group persona learning
            if self.persona_binding and self.plugin_config.persona_binding.enabled:
                self._fire_and_forget(
                    self._check_combined_learning(group_id)
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
                        self._schedule_knowledge_flush(oldest_gid, force=True)

                buf = self._ingestion_buffer.setdefault(group_id, [])
                buf.append(text)
                if len(buf) >= self.plugin_config.knowledge.ingestion_buffer_max:
                    self._schedule_knowledge_flush(group_id, force=True)
                else:
                    self._schedule_knowledge_flush(group_id)

        except Exception as e:
            logger.error(f"[Qunyou] Message processing error: {e}", exc_info=True)

    async def _check_combined_learning(self, group_id: str) -> None:
        """Check if per-group persona learning threshold is reached.

        When a group has a managed persona source, generate versioned local personas;
        otherwise fall back to the regular group persona batch learning.
        """
        if not self.persona_binding or not self.db:
            return

        if not await self.persona_binding.has_managed_persona(group_id, self.db):
            return

        if group_id in self._tone_learning_locks:
            return  # already learning for this group
        self._tone_learning_locks.add(group_id)
        try:
            should_learn = await self.persona_binding.increment_and_check(
                group_id, self.db
            )
            if should_learn:
                await self.persona_binding.run_combined_learning(group_id, self.db)
        finally:
            self._tone_learning_locks.discard(group_id)

    async def _check_persona_learning(self, group_id: str) -> None:
        """Check if supplemental group-persona learning threshold is reached."""
        if self.group_persona and self.db:
            should_learn = await self.group_persona.increment_and_check(
                group_id, self.db
            )
            if should_learn:
                self._fire_and_forget(
                    self.group_persona.run_batch_learning(group_id, self.db)
                )

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

    @filter.command("sbqunyou-getwebtoken")
    @filter.permission_type(PermissionType.ADMIN)
    async def get_webui_token_command(self, event: AstrMessageEvent):
        """生成 WebUI 临时登录令牌: /sbqunyou-getwebtoken"""
        from .webui.api import webui_token_generator
        if not webui_token_generator:
            yield event.plain_result("❌ WebUI 未启动，无法生成令牌")
            return

        token = webui_token_generator()
        ttl = self.plugin_config.webui.web_token_ttl_seconds
        minutes = ttl // 60
        yield event.plain_result(
            f"✅ WebUI 令牌已生成（有效期 {minutes} 分钟）：\n\n`{token}`\n\n请前往 WebUI 管理面板，在登录页面粘贴此令牌完成登录。"
        )

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

    def _schedule_knowledge_flush(self, group_id: str, *, force: bool = False) -> None:
        existing_task = self._ingestion_tasks.get(group_id)
        if existing_task is not None:
            if getattr(existing_task, "_qunyou_sleeping", False):
                existing_task.cancel()
            else:
                return

        delay_seconds = 0.0 if force else max(
            float(self.plugin_config.knowledge.ingestion_cooldown),
            0.0,
        )
        task = asyncio.create_task(
            self._flush_knowledge_buffer(group_id, delay_seconds=delay_seconds)
        )
        setattr(task, "_qunyou_sleeping", delay_seconds > 0)
        self._ingestion_tasks[group_id] = task
        self.background_tasks.add(task)

        def _on_done(done_task: asyncio.Task, gid: str = group_id) -> None:
            if self._ingestion_tasks.get(gid) is done_task:
                self._ingestion_tasks.pop(gid, None)
            self._on_background_task_done(done_task)
            buffered = self._ingestion_buffer.get(gid) or []
            if not done_task.cancelled() and self.knowledge and buffered:
                self._schedule_knowledge_flush(
                    gid,
                    force=len(buffered) >= self.plugin_config.knowledge.ingestion_buffer_max,
                )

        task.add_done_callback(_on_done)

    async def _flush_knowledge_buffer(self, group_id: str, *, delay_seconds: float = 0.0) -> None:
        """Flush buffered messages for a group through LightRAG."""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        current_task = asyncio.current_task()
        if current_task is not None:
            setattr(current_task, "_qunyou_sleeping", False)

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
