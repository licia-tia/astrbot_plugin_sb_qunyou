"""
WebUI 后端 API — FastAPI routes

提供群画像管理、黑话管理、情绪状态查看、活跃线程监控、系统统计等接口。
可独立运行 (uvicorn) 或挂载到 AstrBot 的 web route。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional, TYPE_CHECKING

from astrbot.api import logger

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel as APIModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

if TYPE_CHECKING:
    from ..db.engine import Database


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
        target_tone_version_id: Optional[int] = None
        reviewed_by: Optional[str] = None
        review_notes: str = ""
        created_at: Optional[str] = None
        reviewed_at: Optional[str] = None
        activated_at: Optional[str] = None

    class ReviewDecision(APIModel):
        reviewed_by: Optional[str] = None
        review_notes: str = ""


def _to_review_response(review) -> "ReviewResponse":
    return ReviewResponse(
        id=review.id,
        group_id=review.group_id,
        prompt_type=review.prompt_type,
        status=review.status,
        old_value=review.old_value,
        proposed_value=review.proposed_value,
        change_summary=review.change_summary,
        metadata_json=review.metadata_json,
        target_tone_version_id=review.target_tone_version_id,
        reviewed_by=review.reviewed_by,
        review_notes=review.review_notes,
        created_at=str(review.created_at) if review.created_at else None,
        reviewed_at=str(review.reviewed_at) if review.reviewed_at else None,
        activated_at=str(review.activated_at) if review.activated_at else None,
    )


def create_api(db_getter, config=None) -> "FastAPI":
    """Create the FastAPI app and mount all routes.

    Args:
        db_getter: callable returning the Database instance.
        config: optional WebUIConfig for CORS origins and auth token.
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

    # Auth middleware
    auth_token = config.auth_token if config else None

    if auth_token:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Skip auth for docs and OPTIONS
                if request.url.path in ("/api/docs", "/openapi.json") or request.method == "OPTIONS":
                    return await call_next(request)
                # Skip auth for static files
                if not request.url.path.startswith("/api/"):
                    return await call_next(request)
                auth_header = request.headers.get("authorization", "")
                if not auth_header.startswith("Bearer ") or auth_header[7:] != auth_token:
                    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
                return await call_next(request)

        app.add_middleware(AuthMiddleware)

    def _db() -> "Database":
        d = db_getter()
        if d is None:
            raise HTTPException(503, "Database not available")
        return d

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
        async with db.session() as session:
            from ..db.repo import Repository
            repo = Repository(session)
            ok = await repo.approve_learned_prompt_review(
                review_id,
                reviewed_by=body.reviewed_by,
                review_notes=body.review_notes,
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

    return app
