"""
SQLAlchemy ORM models — ~10 core tables (vs SL's 57)

All vector columns use pgvector's Vector type.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all models."""
    pass


# ---------------------------------------------------------------------------
# 1. raw_messages — 原始消息存储
# ---------------------------------------------------------------------------
class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sender_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sender_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    timestamp: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # embedding stored via pgvector — nullable because we may not always compute it
    embedding = mapped_column(Vector(None), nullable=True)

    __table_args__ = (
        Index("ix_raw_msg_group_ts", "group_id", "timestamp"),
    )


# ---------------------------------------------------------------------------
# 2. group_profiles — 群画像 (千群千面)
# ---------------------------------------------------------------------------
class GroupProfile(Base):
    __tablename__ = "group_profiles"

    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    base_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_whitelist: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    learned_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    learned_prompt_history: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    message_count_since_learn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# 3. active_threads — 活跃话题线程
# ---------------------------------------------------------------------------
class ActiveThread(Base):
    __tablename__ = "active_threads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    topic_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_activity: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # centroid vector for the thread (updated as sliding average)
    centroid = mapped_column(Vector(None), nullable=True)

    # relationships
    messages: Mapped[list["ThreadMessage"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_thread_group_active", "group_id", "is_archived"),
    )


# ---------------------------------------------------------------------------
# 4. thread_messages — 消息 ↔ 线程归属
# ---------------------------------------------------------------------------
class ThreadMessage(Base):
    __tablename__ = "thread_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("raw_messages.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("active_threads.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    thread: Mapped["ActiveThread"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_tm_thread", "thread_id"),
        Index("ix_tm_message", "message_id"),
    )


# ---------------------------------------------------------------------------
# 5. user_memories — 用户记忆 (发言认知)
# ---------------------------------------------------------------------------
class UserMemory(Base):
    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fact: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    embedding = mapped_column(Vector(None), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_mem_group_user", "group_id", "user_id"),
    )


# ---------------------------------------------------------------------------
# 6. emotion_states — 情绪状态
# ---------------------------------------------------------------------------
class EmotionState(Base):
    __tablename__ = "emotion_states"

    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mood: Mapped[str] = mapped_column(String(32), nullable=False, default="neutral")
    valence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    arousal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# 7. jargon_terms — 黑话词条
# ---------------------------------------------------------------------------
class JargonTerm(Base):
    __tablename__ = "jargon_terms"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    term: Mapped[str] = mapped_column(String(128), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_jargon_group_term", "group_id", "term", unique=True),
    )


# ---------------------------------------------------------------------------
# 8. bot_responses — Bot 回复记录
# ---------------------------------------------------------------------------
class BotResponse(Base):
    __tablename__ = "bot_responses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# 9. learning_jobs — 后台学习任务
# ---------------------------------------------------------------------------
class LearningJob(Base):
    __tablename__ = "learning_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)  # persona_learn, jargon_infer, ...
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ---------------------------------------------------------------------------
# 10. learned_prompt_reviews — 学习提示词审核记录
# ---------------------------------------------------------------------------
class LearnedPromptReview(Base):
    __tablename__ = "learned_prompt_reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    prompt_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    old_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    proposed_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    change_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    target_tone_version_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("persona_tone_versions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    review_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at: Mapped[Optional[_dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    target_tone_version: Mapped[Optional["PersonaToneVersion"]] = relationship(
        foreign_keys=[target_tone_version_id], lazy="joined"
    )

    __table_args__ = (
        Index("ix_lpr_group_status", "group_id", "status"),
        Index("ix_lpr_group_type_status", "group_id", "prompt_type", "status"),
    )


# ---------------------------------------------------------------------------
# 11. group_persona_bindings — 群组人格绑定 (独立链路)
# ---------------------------------------------------------------------------
class GroupPersonaBinding(Base):
    __tablename__ = "group_persona_bindings"

    group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    bound_persona_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    active_version_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("persona_tone_versions.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    is_learning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tone_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # relationships
    active_version: Mapped[Optional["PersonaToneVersion"]] = relationship(
        foreign_keys=[active_version_id], lazy="joined"
    )
    tone_versions: Mapped[list["PersonaToneVersion"]] = relationship(
        back_populates="binding",
        foreign_keys="PersonaToneVersion.group_id",
        order_by="PersonaToneVersion.version_num.desc()",
        cascade="all, delete-orphan",
    )


# ---------------------------------------------------------------------------
# 12. persona_tone_versions — 人格语气版本记录
# ---------------------------------------------------------------------------
class PersonaToneVersion(Base):
    __tablename__ = "persona_tone_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("group_persona_bindings.group_id", ondelete="CASCADE"), nullable=False
    )
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    learned_tone: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    binding: Mapped["GroupPersonaBinding"] = relationship(
        back_populates="tone_versions", foreign_keys=[group_id]
    )

    __table_args__ = (
        Index("ix_tone_ver_group_num", "group_id", "version_num", unique=True),
    )
