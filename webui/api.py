"""
WebUI 后端 API — FastAPI routes

提供群画像管理、黑话管理、情绪状态查看、活跃线程监控、系统统计等接口。
可独立运行 (uvicorn) 或挂载到 AstrBot 的 web route。
"""
from __future__ import annotations

import datetime as _dt
import string as _string
from typing import Any, Optional, TYPE_CHECKING

# Module-level token generator set by create_api(), read by main.py for /sbqunyou-getwebtoken command
webui_token_generator: Optional[callable] = None

from astrbot.api import logger

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel as APIModel
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

if TYPE_CHECKING:
    from ..db.engine import Database


_PROMPT_REQUIRED_FIELDS: dict[str, set[str]] = {
    "GROUP_PERSONA_LEARN": {"messages"},
    "COMBINED_LEARNING_PROMPT": {"original_persona", "messages", "count"},
    "INJECTION_GROUP_PERSONA": {"persona"},
    "INJECTION_EMOTION": {"mood"},
    "INJECTION_THREAD_CONTEXT": {"topic", "messages"},
    "INJECTION_USER_MEMORIES": {"user_id", "memories"},
    "INJECTION_JARGON": {"hints"},
}


# ================================================================== #
#  Request / Response models
# ================================================================== #

if HAS_FASTAPI:
    class GroupProfileResponse(APIModel):
        group_id: str
        base_prompt: str
        learned_prompt: str
        source_whitelist: Optional[dict] = None
        message_count_since_learn: int = 0
        updated_at: Optional[str] = None

    class GroupProfileUpdate(APIModel):
        base_prompt: Optional[str] = None
        source_whitelist: Optional[dict] = None

    class JargonResponse(APIModel):
        id: int
        term: str
        meaning: str
        frequency: int
        is_custom: bool

    class JargonCreate(APIModel):
        term: str
        meaning: str

    class EmotionResponse(APIModel):
        group_id: str
        mood: str
        valence: float
        arousal: float
        updated_at: Optional[str] = None

    class ThreadResponse(APIModel):
        id: int
        topic_summary: str
        message_count: int
        last_activity: Optional[str] = None
        is_archived: bool = False

    class StatsResponse(APIModel):
        total_messages: int = 0
        total_groups: int = 0
        active_threads: int = 0
        total_jargon: int = 0
        total_memories: int = 0

    class ReviewResponse(APIModel):
        id: int
        group_id: str
        prompt_type: str
        status: str
        old_value: str
        proposed_value: str
        change_summary: str
        metadata_json: Optional[dict] = None
        target_persona_version_id: Optional[int] = None
        reviewed_by: Optional[str] = None
        review_notes: str = ""
        created_at: Optional[str] = None
        reviewed_at: Optional[str] = None
        activated_at: Optional[str] = None

    class ReviewDecision(APIModel):
        reviewed_by: Optional[str] = None
        review_notes: str = ""

    # ---- Persona Binding models ----

    class PersonaBindingResponse(APIModel):
        group_id: str
        bound_persona_id: Optional[str] = None
        has_base_persona: bool = False
        is_learning_enabled: bool = True
        persona_message_count: int = 0
        active_persona_version_num: Optional[int] = None
        active_persona: Optional[str] = None
        effective_persona_source: Optional[str] = None
        updated_at: Optional[str] = None

    class PersonaVersionResponse(APIModel):
        id: int
        version_num: int
        persona_prompt: str
        is_manual: bool
        created_at: Optional[str] = None
        is_active: bool = False

    class PersonaBindingDetailResponse(PersonaBindingResponse):
        base_persona_prompt: str = ""
        effective_persona: Optional[str] = None
        persona_versions: list[PersonaVersionResponse] = []

    class PersonaBindRequest(APIModel):
        persona_id: Optional[str] = None

    class BasePersonaUpdate(APIModel):
        base_persona_prompt: str = ""

    class PersonaImportRequest(APIModel):
        persona_id: Optional[str] = None

    class PersonaImportResponse(APIModel):
        ok: bool
        persona_id: Optional[str] = None
        imported_length: int = 0

    class AstrBotPersonaResponse(APIModel):
        persona_id: str
        display_name: str
        prompt: str = ""
        effective_prompt: str = ""
        prompt_length: int = 0
        effective_prompt_length: int = 0
        has_persona_slot: bool = False

    class PersonaSlotStatusResponse(APIModel):
        group_id: str
        has_persona_slot: Optional[bool] = None
        checked_at: Optional[str] = None
        system_prompt_length: int = 0
        bound_persona_id: Optional[str] = None
        bound_persona_prompt_available: bool = False
        bound_persona_has_persona_slot: Optional[bool] = None

    class PersonaLearningToggleRequest(APIModel):
        enabled: bool

    class PersonaVersionsClearResponse(APIModel):
        ok: bool
        deleted_versions: int = 0
        superseded_reviews: int = 0

    class LearningJobResponse(APIModel):
        id: int
        group_id: str
        job_type: str
        status: str
        result: Optional[dict] = None
        created_at: Optional[str] = None
        completed_at: Optional[str] = None

    # ---- Prompt management models ----

    class PromptResponse(APIModel):
        string_key: str
        value: str
        description: str
        category: str
        updated_at: Optional[str] = None

    class PromptUpdate(APIModel):
        value: Optional[str] = None
        description: Optional[str] = None

    class PromptResetResponse(APIModel):
        string_key: str
        value: str
        description: str
        category: str

    class LoginRequest(APIModel):
        token: str

    class AuthStatusResponse(APIModel):
        logged_in: bool


def _normalize_review_prompt_type(prompt_type: str) -> str:
    if prompt_type == "tone_version":
        return "persona_version"
    return prompt_type


def _validate_prompt_template(key: str, value: str) -> None:
    required_fields = _PROMPT_REQUIRED_FIELDS.get(key)
    if not required_fields:
        return

    formatter = _string.Formatter()
    try:
        fields = {
            field_name.split(".", 1)[0].split("[", 1)[0]
            for _, field_name, _, _ in formatter.parse(value)
            if field_name
        }
    except ValueError as exc:
        raise HTTPException(400, f"Prompt 模板格式无效: {exc}") from exc

    missing_fields = sorted(required_fields - fields)
    if missing_fields:
        raise HTTPException(
            400,
            f"Prompt 模板缺少占位符: {', '.join(missing_fields)}",
        )


def _infer_effective_persona_source(binding) -> Optional[str]:
    if binding is None:
        return None
    if getattr(binding, "active_version", None) is not None:
        return "active_version"
    if (getattr(binding, "base_persona_prompt", "") or "").strip():
        return "base_persona"
    if getattr(binding, "bound_persona_id", None):
        return "astrbot_persona"
    return None


def _to_persona_version_response(version, active_version_id: Optional[int]) -> "PersonaVersionResponse":
    return PersonaVersionResponse(
        id=version.id,
        version_num=version.version_num,
        persona_prompt=version.learned_tone,
        is_manual=version.is_manual,
        created_at=str(version.created_at) if version.created_at else None,
        is_active=(version.id == active_version_id),
    )


def _to_review_response(review) -> "ReviewResponse":
    return ReviewResponse(
        id=review.id,
        group_id=review.group_id,
        prompt_type=_normalize_review_prompt_type(review.prompt_type),
        status=review.status,
        old_value=review.old_value,
        proposed_value=review.proposed_value,
        change_summary=review.change_summary,
        metadata_json=review.metadata_json,
        target_persona_version_id=review.target_tone_version_id,
        reviewed_by=review.reviewed_by,
        review_notes=review.review_notes,
        created_at=str(review.created_at) if review.created_at else None,
        reviewed_at=str(review.reviewed_at) if review.reviewed_at else None,
        activated_at=str(review.activated_at) if review.activated_at else None,
    )


def create_api(db_getter, config=None, plugin_getter=None) -> "FastAPI":
    """Create the FastAPI app and mount all routes.

    Args:
        db_getter: callable returning the Database instance.
        config: optional WebUIConfig for CORS origins and auth token.
        plugin_getter: optional callable returning the live plugin instance.
    """
    if not HAS_FASTAPI:
        raise RuntimeError("FastAPI not installed — pip install fastapi uvicorn")

    app = FastAPI(
        title="群聊智能体 · 管理面板",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    cors_origins = ["http://localhost:7834", "http://127.0.0.1:7834"]
    if config and hasattr(config, 'cors_origins'):
        cors_origins = config.cors_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth config references — use mutable dict so main.py can update token at runtime
    # Dual-token structure: web_token (10min TTL) + session_token (30day TTL)
    _webui_token_state: dict = {
        "web_token": None,
        "web_token_created_at": None,
        "session_token": None,
        "session_token_created_at": None,
    }
    auth_token = config.auth_token if config else None
    web_token_ttl = getattr(config, "web_token_ttl_seconds", 600) if config else 600
    session_token_ttl_days = getattr(config, "session_token_ttl_days", 30) if config else 30
    session_token_ttl = session_token_ttl_days * 24 * 3600

    def _db() -> "Database":
        d = db_getter()
        if d is None:
            raise HTTPException(503, "Database not available")
        return d

    def _plugin():
        return plugin_getter() if plugin_getter else None

    def _check_auth(request) -> bool:
        """Return True if request is authorized via session_token (30day TTL) or static auth_token."""
        import time
        if not auth_token:
            return True
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]

        # Bearer token (static, deprecated)
        if auth_token and token == auth_token:
            return True

        # Session token (runtime-generated, 30day TTL)
        current_token = _webui_token_state["session_token"]
        created_at = _webui_token_state["session_token_created_at"]
        if current_token and token == current_token:
            if created_at is not None:
                if time.time() - created_at > session_token_ttl:
                    return False  # expired
            return True

        return False

    async def _run_manual_combined_learning(group_id: str) -> None:
        plugin = _plugin()
        if not plugin or not getattr(plugin, "persona_binding", None) or not getattr(plugin, "db", None):
            return
        try:
            await plugin.persona_binding.run_combined_learning(group_id, plugin.db)
        finally:
            plugin._tone_learning_locks.discard(group_id)

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # Skip auth for docs, OPTIONS, and auth endpoints (including exchange)
            if request.url.path in (
                "/api/docs", "/openapi.json", "/api/auth/login",
                "/api/auth/status", "/api/auth/exchange",
            ) or request.method == "OPTIONS":
                return await call_next(request)
            # Skip auth for static files
            if not request.url.path.startswith("/api/"):
                return await call_next(request)
            if not _check_auth(request):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
            return await call_next(request)

    app.add_middleware(AuthMiddleware)

    # ---- Auth Endpoints ----

    @app.post("/api/auth/exchange")
    async def exchange_token(body: LoginRequest):
        """Exchange a valid web_token (10min TTL) for a session_token (30day TTL)."""
        import secrets
        import time
        import datetime as dt
        web_token = _webui_token_state["web_token"]
        web_created = _webui_token_state["web_token_created_at"]
        if not web_token or web_created is None:
            return JSONResponse(status_code=401, content={"error": "invalid_or_expired_web_token"})
        if time.time() - web_created > web_token_ttl:
            return JSONResponse(status_code=401, content={"error": "invalid_or_expired_web_token"})
        if body.token != web_token:
            return JSONResponse(status_code=401, content={"error": "invalid_or_expired_web_token"})
        # Issue session_token and consume web_token
        session_token = secrets.token_hex(32)
        _webui_token_state["session_token"] = session_token
        _webui_token_state["session_token_created_at"] = time.time()
        # Clear web_token so it cannot be reused
        _webui_token_state["web_token"] = None
        _webui_token_state["web_token_created_at"] = None
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=session_token_ttl)
        return {"session_token": session_token, "expires_at": expires_at.isoformat()}

    @app.post("/api/auth/login", response_model=AuthStatusResponse)
    async def login(body: LoginRequest):
        """Verify if a session_token is currently valid."""
        import time
        if auth_token and body.token == auth_token:
            return AuthStatusResponse(logged_in=True)
        session_token = _webui_token_state["session_token"]
        created_at = _webui_token_state["session_token_created_at"]
        if session_token and body.token == session_token and created_at is not None:
            if time.time() - created_at <= session_token_ttl:
                return AuthStatusResponse(logged_in=True)
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})

    @app.get("/api/auth/status", response_model=AuthStatusResponse)
    async def auth_status(request: Request):
        """Check whether the caller's bearer token is currently valid."""
        return AuthStatusResponse(logged_in=_check_auth(request))

    @app.post("/api/auth/logout")
    async def logout():
        """Clear session_token, allowing the same web_token to be exchanged again."""
        _webui_token_state["session_token"] = None
        _webui_token_state["session_token_created_at"] = None
        return {"ok": True}

    def _generate_webui_token() -> str:
        """Generate a new web_token (10min TTL) for exchange at the WebUI."""
        import secrets
        import time
        _webui_token_state["web_token"] = secrets.token_hex(16)
        _webui_token_state["web_token_created_at"] = time.time()
        # Do NOT set session_token here
        return _webui_token_state["web_token"]

    # Store generator on module-level variable for main.py access
    global webui_token_generator
    webui_token_generator = _generate_webui_token

    # ------------------------------------------------------------------ #
    #  Groups
    # ------------------------------------------------------------------ #

    @app.get("/api/groups", response_model=list[GroupProfileResponse])
    async def list_groups():
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            profiles = await repo.list_all_groups()
            return [
                GroupProfileResponse(
                    group_id=p.group_id,
                    base_prompt=p.base_prompt,
                    learned_prompt=p.learned_prompt,
                    source_whitelist=p.source_whitelist,
                    message_count_since_learn=p.message_count_since_learn,
                    updated_at=str(p.updated_at) if p.updated_at else None,
                )
                for p in profiles
            ]

    @app.get("/api/groups/{group_id}", response_model=GroupProfileResponse)
    async def get_group(group_id: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            profile = await repo.get_or_create_group_profile(group_id)
            await session.commit()
            return GroupProfileResponse(
                group_id=profile.group_id,
                base_prompt=profile.base_prompt,
                learned_prompt=profile.learned_prompt,
                source_whitelist=profile.source_whitelist,
                message_count_since_learn=profile.message_count_since_learn,
                updated_at=str(profile.updated_at) if profile.updated_at else None,
            )

    @app.put("/api/groups/{group_id}")
    async def update_group(group_id: str, body: GroupProfileUpdate):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_group_profile(group_id)
            await repo.update_group_profile(
                group_id,
                base_prompt=body.base_prompt,
                source_whitelist=body.source_whitelist,
            )
            await session.commit()
        return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Threads
    # ------------------------------------------------------------------ #

    @app.get("/api/groups/{group_id}/threads", response_model=list[ThreadResponse])
    async def list_threads(group_id: str, include_archived: bool = False):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            from ..db.models import ActiveThread
            from sqlalchemy import select
            repo = Repository(session)
            if include_archived:
                stmt = (
                    select(ActiveThread)
                    .where(ActiveThread.group_id == group_id)
                    .order_by(ActiveThread.last_activity.desc())
                    .limit(50)
                )
                result = await session.execute(stmt)
                threads = result.scalars().all()
            else:
                threads = await repo.get_active_threads(group_id, limit=50)
            return [
                ThreadResponse(
                    id=t.id,
                    topic_summary=t.topic_summary,
                    message_count=t.message_count,
                    last_activity=str(t.last_activity) if t.last_activity else None,
                    is_archived=t.is_archived,
                )
                for t in threads
            ]

    # ------------------------------------------------------------------ #
    #  Emotion
    # ------------------------------------------------------------------ #

    @app.get("/api/groups/{group_id}/emotion", response_model=EmotionResponse)
    async def get_emotion(group_id: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            state = await repo.get_or_create_emotion(group_id)
            await session.commit()
            return EmotionResponse(
                group_id=state.group_id,
                mood=state.mood,
                valence=state.valence,
                arousal=state.arousal,
                updated_at=str(state.updated_at) if state.updated_at else None,
            )

    # ------------------------------------------------------------------ #
    #  Jargon
    # ------------------------------------------------------------------ #

    @app.get("/api/groups/{group_id}/jargon", response_model=list[JargonResponse])
    async def list_jargon(group_id: str, min_freq: int = 1):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            terms = await repo.get_group_jargon(group_id, min_frequency=min_freq)
            return [
                JargonResponse(
                    id=t.id,
                    term=t.term,
                    meaning=t.meaning,
                    frequency=t.frequency,
                    is_custom=t.is_custom,
                )
                for t in terms
            ]

    @app.post("/api/groups/{group_id}/jargon", response_model=JargonResponse)
    async def add_custom_jargon(group_id: str, body: JargonCreate):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            jid = await repo.add_custom_jargon(group_id, body.term, body.meaning)
            await session.commit()
            return JargonResponse(
                id=jid,
                term=body.term,
                meaning=body.meaning,
                frequency=0,
                is_custom=True,
            )

    @app.delete("/api/groups/{group_id}/jargon/{jargon_id}")
    async def delete_jargon(group_id: str, jargon_id: int):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            # Verify ownership before deleting
            from sqlalchemy import select
            from ..db.models import JargonTerm
            stmt = select(JargonTerm).where(
                JargonTerm.id == jargon_id,
                JargonTerm.group_id == group_id,
            )
            result = await session.execute(stmt)
            if result.scalar_one_or_none() is None:
                raise HTTPException(404, "Jargon term not found in this group")
            await repo.delete_jargon(jargon_id)
            await session.commit()
        return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Memories
    # ------------------------------------------------------------------ #

    @app.get("/api/groups/{group_id}/memories/{user_id}")
    async def list_memories(group_id: str, user_id: str, limit: int = 20):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            memories = await repo.get_user_memories(group_id, user_id, limit=limit)
            return [
                {
                    "id": m.id,
                    "fact": m.fact,
                    "importance": m.importance,
                    "created_at": str(m.created_at) if m.created_at else None,
                }
                for m in memories
            ]

    # ------------------------------------------------------------------ #
    #  Stats
    # ------------------------------------------------------------------ #

    @app.get("/api/stats", response_model=StatsResponse)
    async def get_stats():
        db = _db()
        async with db.session() as session:
            from sqlalchemy import select, func
            from ..db.models import (
                RawMessage, GroupProfile, ActiveThread, JargonTerm, UserMemory
            )

            total_messages = (await session.execute(
                select(func.count()).select_from(RawMessage)
            )).scalar_one()

            total_groups = (await session.execute(
                select(func.count()).select_from(GroupProfile)
            )).scalar_one()

            active_threads = (await session.execute(
                select(func.count()).select_from(ActiveThread)
                .where(ActiveThread.is_archived == False)  # noqa
            )).scalar_one()

            total_jargon = (await session.execute(
                select(func.count()).select_from(JargonTerm)
            )).scalar_one()

            total_memories = (await session.execute(
                select(func.count()).select_from(UserMemory)
            )).scalar_one()

            return StatsResponse(
                total_messages=total_messages,
                total_groups=total_groups,
                active_threads=active_threads,
                total_jargon=total_jargon,
                total_memories=total_memories,
            )

    # ------------------------------------------------------------------ #
    #  Reviews
    # ------------------------------------------------------------------ #

    @app.get("/api/reviews/pending", response_model=list[ReviewResponse])
    async def list_pending_reviews(group_id: str | None = None, prompt_type: str | None = None, limit: int = 50):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            reviews = await repo.get_pending_learned_prompt_reviews(
                group_id=group_id,
                prompt_type=prompt_type,
                limit=limit,
            )
            return [_to_review_response(review) for review in reviews]

    @app.get("/api/reviews/history/{group_id}", response_model=list[ReviewResponse])
    async def get_review_history(group_id: str, prompt_type: str | None = None, limit: int = 50):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            reviews = await repo.get_review_history(group_id, prompt_type=prompt_type, limit=limit)
            return [_to_review_response(review) for review in reviews]

    @app.post("/api/reviews/{review_id}/approve")
    async def approve_review(review_id: int, body: ReviewDecision):
        db = _db()
        plugin = _plugin()
        max_group_history_versions = 10
        if plugin and getattr(plugin, "plugin_config", None):
            max_group_history_versions = plugin.plugin_config.group_persona.max_history_versions
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            ok = await repo.approve_learned_prompt_review(
                review_id,
                reviewed_by=body.reviewed_by,
                review_notes=body.review_notes,
                max_group_history_versions=max_group_history_versions,
            )
            await session.commit()
        if not ok:
            raise HTTPException(404, "Review not found or cannot be approved")
        return {"ok": True}

    @app.post("/api/reviews/{review_id}/reject")
    async def reject_review(review_id: int, body: ReviewDecision):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            ok = await repo.reject_learned_prompt_review(
                review_id,
                reviewed_by=body.reviewed_by,
                review_notes=body.review_notes,
            )
            await session.commit()
        if not ok:
            raise HTTPException(404, "Review not found or cannot be rejected")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Persona Bindings
    # ------------------------------------------------------------------ #

    @app.get("/api/persona-bindings", response_model=list[PersonaBindingResponse])
    async def list_persona_bindings():
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            bindings = await repo.get_all_persona_bindings()
            return [
                PersonaBindingResponse(
                    group_id=b.group_id,
                    bound_persona_id=b.bound_persona_id,
                    has_base_persona=bool((b.base_persona_prompt or "").strip()),
                    is_learning_enabled=b.is_learning_enabled,
                    persona_message_count=b.tone_message_count,
                    active_persona_version_num=b.active_version.version_num if b.active_version else None,
                    active_persona=b.active_version.learned_tone if b.active_version else None,
                    effective_persona_source=_infer_effective_persona_source(b),
                    updated_at=str(b.updated_at) if b.updated_at else None,
                )
                for b in bindings
            ]

    @app.get("/api/astrbot-personas", response_model=list[AstrBotPersonaResponse])
    async def list_astrbot_personas():
        plugin = _plugin()
        if not plugin or not getattr(plugin, "persona_binding", None):
            return []

        personas = await plugin.persona_binding.list_persona_catalog()
        return [AstrBotPersonaResponse(**persona) for persona in personas]

    @app.get("/api/persona-bindings/{group_id}", response_model=PersonaBindingDetailResponse)
    async def get_persona_binding(group_id: str):
        db = _db()
        plugin = _plugin()
        effective_persona = None
        effective_persona_source = None
        if plugin and getattr(plugin, "persona_binding", None) and getattr(plugin, "db", None):
            effective_persona, effective_persona_source = await plugin.persona_binding.resolve_effective_persona_prompt(
                group_id, plugin.db
            )
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)
            if binding is None:
                return PersonaBindingDetailResponse(
                    group_id=group_id,
                    bound_persona_id=None,
                    has_base_persona=False,
                    is_learning_enabled=True,
                    persona_message_count=0,
                    active_persona_version_num=None,
                    active_persona=None,
                    effective_persona_source=effective_persona_source,
                    updated_at=None,
                    base_persona_prompt="",
                    effective_persona=effective_persona,
                    persona_versions=[],
                )
            versions = await repo.get_persona_versions(group_id)
            if effective_persona_source is None:
                effective_persona_source = _infer_effective_persona_source(binding)
            if effective_persona is None:
                if binding.active_version is not None:
                    effective_persona = binding.active_version.learned_tone
                elif (binding.base_persona_prompt or "").strip():
                    effective_persona = binding.base_persona_prompt
            return PersonaBindingDetailResponse(
                group_id=binding.group_id,
                bound_persona_id=binding.bound_persona_id,
                has_base_persona=bool((binding.base_persona_prompt or "").strip()),
                is_learning_enabled=binding.is_learning_enabled,
                persona_message_count=binding.tone_message_count,
                active_persona_version_num=binding.active_version.version_num if binding.active_version else None,
                active_persona=binding.active_version.learned_tone if binding.active_version else None,
                effective_persona_source=effective_persona_source,
                updated_at=str(binding.updated_at) if binding.updated_at else None,
                base_persona_prompt=binding.base_persona_prompt,
                effective_persona=effective_persona,
                persona_versions=[
                    _to_persona_version_response(v, binding.active_version_id)
                    for v in versions
                ],
            )

    @app.put("/api/persona-bindings/{group_id}/base-persona")
    async def update_base_persona(group_id: str, body: BasePersonaUpdate):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, base_persona_prompt=body.base_persona_prompt)
            await session.commit()
        return {"ok": True}

    @app.put("/api/persona-bindings/{group_id}/bind")
    async def bind_persona(group_id: str, body: PersonaBindRequest):
        db = _db()
        plugin = _plugin()
        persona_id = (body.persona_id or "").strip() or None
        if persona_id and plugin and getattr(plugin, "persona_binding", None) and getattr(plugin, "db", None):
            slot_info = await plugin.persona_binding.inspect_bound_persona_slot(
                group_id,
                plugin.db,
                persona_id=persona_id,
            )
            if not slot_info.get("prompt_available", False):
                raise HTTPException(404, "AstrBot persona not found or unavailable")
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, bound_persona_id=persona_id)
            await session.commit()
        return {"ok": True}

    @app.post("/api/persona-bindings/{group_id}/import-base-persona", response_model=PersonaImportResponse)
    async def import_base_persona(group_id: str, body: PersonaImportRequest):
        plugin = _plugin()
        if not plugin or not getattr(plugin, "persona_binding", None) or not getattr(plugin, "db", None):
            raise HTTPException(503, "Persona binding service not available")

        ok, persona_id, imported_prompt = await plugin.persona_binding.import_base_persona_from_astrbot(
            group_id,
            plugin.db,
            persona_id=body.persona_id,
        )
        if not ok:
            raise HTTPException(404, "AstrBot persona not found or unavailable")

        return PersonaImportResponse(
            ok=True,
            persona_id=persona_id,
            imported_length=len(imported_prompt),
        )

    @app.get("/api/persona-bindings/{group_id}/slot-status", response_model=PersonaSlotStatusResponse)
    async def get_persona_slot_status(group_id: str):
        db = _db()
        plugin = _plugin()
        runtime_status = {}
        if plugin is not None:
            runtime_status = getattr(plugin, "_prompt_slot_status", {}).get(group_id, {})

        bound_persona_id = None
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)
            if binding is not None:
                bound_persona_id = binding.bound_persona_id

        bound_slot = {
            "prompt_available": False,
            "has_persona_slot": False,
        }
        if plugin and getattr(plugin, "persona_binding", None) and getattr(plugin, "db", None):
            bound_slot = await plugin.persona_binding.inspect_bound_persona_slot(group_id, plugin.db)

        return PersonaSlotStatusResponse(
            group_id=group_id,
            has_persona_slot=runtime_status.get("has_persona_slot"),
            checked_at=runtime_status.get("checked_at"),
            system_prompt_length=int(runtime_status.get("system_prompt_length", 0) or 0),
            bound_persona_id=bound_slot.get("persona_id") or bound_persona_id,
            bound_persona_prompt_available=bool(bound_slot.get("prompt_available", False)),
            bound_persona_has_persona_slot=bound_slot.get("has_persona_slot"),
        )

    @app.put("/api/persona-bindings/{group_id}/learning-toggle")
    @app.put("/api/persona-bindings/{group_id}/tone-toggle", include_in_schema=False)
    async def toggle_persona_learning(group_id: str, body: PersonaLearningToggleRequest):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            await repo.get_or_create_persona_binding(group_id)
            await repo.update_persona_binding(group_id, is_learning_enabled=body.enabled)
            await session.commit()
        return {"ok": True}

    @app.post("/api/persona-bindings/{group_id}/learn")
    @app.post("/api/persona-bindings/{group_id}/tone-learn", include_in_schema=False)
    async def trigger_persona_learning(group_id: str):
        plugin = _plugin()
        if not plugin or not getattr(plugin, "persona_binding", None) or not getattr(plugin, "db", None):
            raise HTTPException(503, "Persona binding service not available")
        if not await plugin.persona_binding.has_managed_persona(group_id, plugin.db):
            raise HTTPException(400, "该群尚未配置可学习的人格来源")
        if group_id in plugin._tone_learning_locks:
            return {"ok": False, "message": "该群已有学习任务进行中，请稍后刷新状态"}

        plugin._tone_learning_locks.add(group_id)
        plugin._fire_and_forget(_run_manual_combined_learning(group_id))
        return {"ok": True, "message": "每群人格学习已启动，请稍候刷新状态"}

    @app.delete("/api/persona-bindings/{group_id}/versions", response_model=PersonaVersionsClearResponse)
    @app.delete("/api/persona-bindings/{group_id}/tone-versions", include_in_schema=False)
    async def clear_persona_versions(group_id: str):
        plugin = _plugin()
        if plugin and group_id in getattr(plugin, "_tone_learning_locks", set()):
            raise HTTPException(409, "该群有人格学习任务进行中，暂时不能清空版本历史")

        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)
            if binding is None:
                raise HTTPException(404, "Persona binding not found")

            deleted_versions, superseded_reviews = await repo.clear_all_persona_versions(group_id)
            await session.commit()

        return PersonaVersionsClearResponse(
            ok=True,
            deleted_versions=deleted_versions,
            superseded_reviews=superseded_reviews,
        )

    @app.put("/api/persona-bindings/{group_id}/versions/{version_num}/activate")
    @app.put("/api/persona-bindings/{group_id}/tone-switch/{version_num}", include_in_schema=False)
    async def activate_persona_version(group_id: str, version_num: int):
        db = _db()
        plugin = _plugin()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            versions = await repo.get_persona_versions(group_id)
            target_version = next((v for v in versions if v.version_num == version_num), None)
            if target_version is None:
                raise HTTPException(404, f"人格版本 V{version_num} 不存在")

            review_gate_enabled = bool(
                plugin and getattr(plugin, "plugin_config", None)
                and plugin.plugin_config.review_gate.enabled_for_tone
            )
            if review_gate_enabled:
                pending_reviews = await repo.get_pending_learned_prompt_reviews(
                    group_id=group_id,
                    prompt_type="persona_version",
                    limit=20,
                )
                if any(review.target_tone_version_id == target_version.id for review in pending_reviews):
                    raise HTTPException(409, "该人格版本仍在审核中，不能直接激活")

            success = await repo.set_active_persona_version_by_num(group_id, version_num)
            await session.commit()
        if not success:
            raise HTTPException(404, f"人格版本 V{version_num} 不存在")
        return {"ok": True}

    @app.get("/api/persona-bindings/{group_id}/versions", response_model=list[PersonaVersionResponse])
    @app.get("/api/persona-bindings/{group_id}/tone-versions", response_model=list[PersonaVersionResponse], include_in_schema=False)
    async def list_persona_versions(group_id: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)
            versions = await repo.get_persona_versions(group_id)
            return [
                _to_persona_version_response(v, binding.active_version_id if binding else None)
                for v in versions
            ]

    @app.delete("/api/persona-bindings/{group_id}")
    async def delete_persona_binding(group_id: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            binding = await repo.get_persona_binding(group_id)
            if binding is None:
                raise HTTPException(404, "Persona binding not found")
            ok = await repo.delete_persona_binding(group_id)
            await session.commit()
        if not ok:
            raise HTTPException(500, "Failed to delete persona binding")
        return {"ok": True}

    # ------------------------------------------------------------------ #
    #  Learning Jobs
    # ------------------------------------------------------------------ #

    @app.get("/api/learning-jobs", response_model=list[LearningJobResponse])
    async def list_learning_jobs(
        group_id: str | None = None,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
    ):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            jobs = await repo.get_learning_jobs(
                group_id=group_id,
                status=status,
                job_type=job_type,
                limit=limit,
            )
            return [
                LearningJobResponse(
                    id=j.id,
                    group_id=j.group_id,
                    job_type=j.job_type,
                    status=j.status,
                    result=j.result,
                    created_at=str(j.created_at) if j.created_at else None,
                    completed_at=str(j.completed_at) if j.completed_at else None,
                )
                for j in jobs
            ]

    @app.get("/api/learning-jobs/{job_id}", response_model=LearningJobResponse)
    async def get_learning_job(job_id: int):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            from ..db.models import LearningJob
            from sqlalchemy import select
            stmt = select(LearningJob).where(LearningJob.id == job_id)
            result = await session.execute(stmt)
            job = result.scalar_one_or_none()
            if job is None:
                raise HTTPException(404, "Learning job not found")
            return LearningJobResponse(
                id=job.id,
                group_id=job.group_id,
                job_type=job.job_type,
                status=job.status,
                result=job.result,
                created_at=str(job.created_at) if job.created_at else None,
                completed_at=str(job.completed_at) if job.completed_at else None,
            )

    # ------------------------------------------------------------------ #
    #  Prompt Management
    # ------------------------------------------------------------------ #

    @app.get("/api/prompts", response_model=list[PromptResponse])
    async def list_prompts(category: str | None = None):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            if category:
                prompts = await repo.get_prompts_by_category(category)
            else:
                prompts = await repo.list_all_prompts()
            return [
                PromptResponse(
                    string_key=p.string_key,
                    value=p.value,
                    description=p.description,
                    category=p.category,
                    updated_at=str(p.updated_at) if p.updated_at else None,
                )
                for p in prompts
            ]

    @app.get("/api/prompts/{key}", response_model=PromptResponse)
    async def get_prompt(key: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            prompt = await repo.get_prompt(key)
            if not prompt:
                raise HTTPException(404, "Prompt not found")
            return PromptResponse(
                string_key=prompt.string_key,
                value=prompt.value,
                description=prompt.description,
                category=prompt.category,
                updated_at=str(prompt.updated_at) if prompt.updated_at else None,
            )

    @app.put("/api/prompts/{key}")
    async def update_prompt(key: str, body: PromptUpdate):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            from ..db.models import SystemPrompt
            from sqlalchemy import update
            repo = Repository(session)
            existing = await repo.get_prompt(key)
            if not existing:
                raise HTTPException(404, "Prompt not found")
            update_vals = {}
            if body.value is not None:
                _validate_prompt_template(key, body.value)
                update_vals["value"] = body.value
            if body.description is not None:
                update_vals["description"] = body.description
            if update_vals:
                stmt = (
                    update(SystemPrompt)
                    .where(SystemPrompt.string_key == key)
                    .values(**update_vals)
                )
                await session.execute(stmt)
                await session.commit()
        return {"ok": True}

    @app.post("/api/prompts/{key}/reset", response_model=PromptResetResponse)
    async def reset_prompt(key: str):
        db = _db()
        async with db.session() as session:
            from ..db.repo import Repository
            from ..prompts.seed_data import SEED_PROMPTS
            repo = Repository(session)
            seed_entry = next((p for p in SEED_PROMPTS if p["string_key"] == key), None)
            if not seed_entry:
                raise HTTPException(404, "Prompt not found in seed data")
            await repo.upsert_prompt(
                string_key=key,
                value=seed_entry["value"],
                description=seed_entry["description"],
                category=seed_entry["category"],
            )
            await session.commit()
            return PromptResetResponse(
                string_key=key,
                value=seed_entry["value"],
                description=seed_entry["description"],
                category=seed_entry["category"],
            )

    return app
