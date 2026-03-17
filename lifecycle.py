"""
插件生命周期编排 — bootstrap / on_load / shutdown

Lazy-init pattern from self-learning issue #78: Reranker and LightRAG
are resolved in on_load() (Phase 2) when providers are registered,
not in bootstrap() (Phase 1) when inst_map is empty.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import os
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.star import Context

from .config import PluginConfig
from .db.engine import Database

if TYPE_CHECKING:
    from .main import QunyouPlugin


class Lifecycle:
    """Orchestrates plugin initialization, startup, and shutdown."""

    def __init__(self, plugin: "QunyouPlugin") -> None:
        self._plugin = plugin
        self._db: Database | None = None
        self._webui_server = None

    # ------------------------------------------------------------------ #
    #  Phase 1: synchronous bootstrap (called from __init__)
    # ------------------------------------------------------------------ #

    def bootstrap(self, config: PluginConfig, context: Context) -> None:
        """Create service instances (no I/O here)."""
        p = self._plugin

        # Database
        self._db = Database(config.database)
        p.db = self._db

        # LLM Adapter
        from .services.llm_adapter import LLMAdapter
        p.llm = LLMAdapter(context, config)

        # Services
        from .services.group_persona import GroupPersonaService
        from .services.speaker_memory import SpeakerMemoryService
        from .services.emotion import EmotionEngine
        from .services.jargon import JargonService
        from .services.hook_handler import HookHandler
        from .services.persona_binding import PersonaBindingService

        p.group_persona = GroupPersonaService(config, p.llm)
        p.speaker_memory = SpeakerMemoryService(config, p.llm)
        p.emotion = EmotionEngine(config, p.llm)
        p.jargon = JargonService(config, p.llm)
        p.persona_binding = PersonaBindingService(config, p.llm)
        p.hook_handler = HookHandler(config, p)

        # Pipeline
        from .pipeline.debounce import DebounceManager
        from .pipeline.topic_router import TopicThreadRouter

        p.debounce = DebounceManager(config.debounce, llm=p.llm)
        p.topic_router = TopicThreadRouter(config.topic, p.llm)

        logger.info("[Lifecycle] Bootstrap complete — all services created")

    # ------------------------------------------------------------------ #
    #  Phase 2: async startup (called from initialize())
    # ------------------------------------------------------------------ #

    async def on_load(self) -> None:
        """Start database, WebUI, and async services.

        Reranker and LightRAG are initialized here (not in bootstrap)
        because AstrBot's provider_manager.inst_map is populated
        only after all plugins are loaded (Issue #78).
        """
        config = self._plugin.plugin_config
        p = self._plugin

        # 1. Database
        if self._db:
            try:
                await self._db.start()
                logger.info("[Lifecycle] Database started")
            except Exception as e:
                logger.error(f"[Lifecycle] Database start failed: {e}", exc_info=True)

        # 2. Reranker (lazy-init, Issue #78)
        self._init_reranker(config, p)

        # 3. LightRAG Knowledge Engine (lazy-init)
        self._init_knowledge(config, p)

        # 3.5 Warmup LightRAG for recently active groups
        if getattr(p, 'knowledge', None) and self._db:
            try:
                async with self._db.session() as session:
                    from .db.repo import Repository
                    repo = Repository(session)
                    active_groups = await repo.get_recently_active_group_ids(
                        since=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=7),
                        limit=config.knowledge.warmup_active_groups_limit,
                    )
                if active_groups:
                    await p.knowledge.warmup(active_groups)
                    logger.info(
                        f"[Lifecycle] LightRAG pre-warmed {len(active_groups)} active groups"
                    )
            except Exception as e:
                logger.debug(f"[Lifecycle] LightRAG warmup failed: {e}")

        # 4. Background retry if providers not yet available
        if not getattr(p, 'reranker', None) or (
            config.knowledge.engine == 'lightrag'
            and not getattr(p, 'knowledge', None)
        ):
            task = asyncio.create_task(
                self._lazy_resolve_providers(config, p)
            )
            p.background_tasks.add(task)
            task.add_done_callback(p.background_tasks.discard)

        # 5. WebUI
        if config.webui.enabled:
            await self._start_webui(config)

        logger.info("[Lifecycle] Plugin loaded successfully")

    def _init_reranker(self, config: PluginConfig, p: "QunyouPlugin") -> None:
        """Try to resolve reranker provider."""
        if not config.rerank.enabled or not config.rerank.provider_id:
            p.reranker = None
            return
        try:
            from .services.reranker import RerankProviderFactory
            p.reranker = RerankProviderFactory.create(
                config.rerank.provider_id,
                p.context,
            )
        except Exception as e:
            logger.warning(f"[Lifecycle] Reranker init failed: {e}")
            p.reranker = None

    def _init_knowledge(self, config: PluginConfig, p: "QunyouPlugin") -> None:
        """Try to create LightRAG knowledge manager."""
        if config.knowledge.engine != "lightrag":
            p.knowledge = None
            return
        try:
            from .services.knowledge import LightRAGKnowledgeManager
            mgr = LightRAGKnowledgeManager(config, p.llm)
            if mgr.available:
                p.knowledge = mgr
                logger.info("[Lifecycle] LightRAG knowledge engine ready")
            else:
                p.knowledge = None
                logger.warning(
                    "[Lifecycle] LightRAG not available (lightrag-hku not installed)"
                )
        except Exception as e:
            logger.warning(f"[Lifecycle] LightRAG init failed: {e}")
            p.knowledge = None

    async def _lazy_resolve_providers(
        self, config: PluginConfig, p: "QunyouPlugin"
    ) -> None:
        """Background retry loop for providers not yet available (Issue #78)."""
        for attempt in range(30):
            await asyncio.sleep(3)

            if not getattr(p, 'reranker', None) and config.rerank.enabled:
                self._init_reranker(config, p)

            if (
                not getattr(p, 'knowledge', None)
                and config.knowledge.engine == 'lightrag'
            ):
                self._init_knowledge(config, p)

            # All resolved?
            reranker_ok = (
                getattr(p, 'reranker', None) is not None
                or not config.rerank.enabled
            )
            knowledge_ok = (
                getattr(p, 'knowledge', None) is not None
                or config.knowledge.engine == 'off'
            )
            if reranker_ok and knowledge_ok:
                logger.info(
                    f"[Lifecycle] Lazy provider resolution done "
                    f"(attempt {attempt + 1})"
                )
                return

        logger.warning(
            "[Lifecycle] Lazy provider resolution timed out (90s)"
        )

    async def _start_webui(self, config: PluginConfig) -> None:
        """Start the WebUI server in background."""
        try:
            from .webui.api import create_api, HAS_FASTAPI
            if not HAS_FASTAPI:
                logger.warning("[Lifecycle] FastAPI not installed, WebUI disabled")
                return

            app = create_api(lambda: self._db, config=config.webui)

            # Serve static frontend
            from fastapi.staticfiles import StaticFiles
            frontend_dir = os.path.join(
                os.path.dirname(__file__), "webui", "frontend"
            )
            if os.path.isdir(frontend_dir):
                app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

            import uvicorn
            uv_config = uvicorn.Config(
                app,
                host=config.webui.host,
                port=config.webui.port,
                log_level="warning",
            )
            self._webui_server = uvicorn.Server(uv_config)

            task = asyncio.create_task(self._webui_server.serve())
            self._plugin.background_tasks.add(task)
            task.add_done_callback(self._plugin.background_tasks.discard)
            logger.info(
                f"[Lifecycle] WebUI started at "
                f"http://{config.webui.host}:{config.webui.port}"
            )
        except Exception as e:
            logger.error(f"[Lifecycle] WebUI start failed: {e}", exc_info=True)

    # ------------------------------------------------------------------ #
    #  Phase 3: ordered shutdown
    # ------------------------------------------------------------------ #

    async def shutdown(self) -> None:
        """Orderly shutdown: Knowledge → WebUI → tasks → DB."""
        p = self._plugin
        logger.info("[Lifecycle] Shutdown started")

        # 0. Close LightRAG
        knowledge = getattr(p, 'knowledge', None)
        if knowledge:
            try:
                if hasattr(knowledge, 'finalize'):
                    await knowledge.finalize()
                await knowledge.close()
                logger.info("[Lifecycle] LightRAG closed")
            except Exception as e:
                logger.error(f"[Lifecycle] LightRAG close error: {e}")

        # 1. Stop WebUI
        if self._webui_server:
            try:
                self._webui_server.should_exit = True
                logger.info("[Lifecycle] WebUI stopped")
            except Exception as e:
                logger.error(f"[Lifecycle] WebUI stop error: {e}")

        # 2. Cancel background tasks
        for task in list(p.background_tasks):
            if not task.done():
                task.cancel()
        if p.background_tasks:
            await asyncio.gather(*p.background_tasks, return_exceptions=True)
            p.background_tasks.clear()
        logger.info("[Lifecycle] Background tasks cancelled")

        # 3. Stop database
        if self._db:
            try:
                await self._db.stop()
            except Exception as e:
                logger.error(f"[Lifecycle] DB stop error: {e}")

        logger.info("[Lifecycle] Shutdown complete")
