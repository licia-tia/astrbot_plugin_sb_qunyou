"""
Unified Repository — thin CRUD layer over all ORM models.

SL had 24 separate repository files.  We keep a single class that groups
related queries by docstring sections.  Each public method is a thin async
helper around an SQLAlchemy query.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional, Sequence

from sqlalchemy import delete, select, update, func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .models import (
    ActiveThread,
    BotResponse,
    EmotionState,
    GroupPersonaBinding,
    GroupProfile,
    JargonTerm,
    LearnedPromptReview,
    LearningJob,
    PersonaToneVersion,
    RawMessage,
    ThreadMessage,
    UserMemory,
)


class Repository:
    """All database operations in one place."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ---- helpers ----

    async def commit(self) -> None:
        await self._s.commit()

    async def flush(self) -> None:
        await self._s.flush()

    # =====================================================================
    #  RawMessage
    # =====================================================================

    async def save_raw_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text_content: str,
        platform: str = "",
        embedding: Any = None,
        timestamp: _dt.datetime | None = None,
    ) -> int:
        msg = RawMessage(
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text_content,
            platform=platform,
            embedding=embedding,
            timestamp=timestamp or _dt.datetime.now(_dt.timezone.utc),
        )
        self._s.add(msg)
        await self._s.flush()
        return msg.id

    async def get_recent_messages(
        self, group_id: str, limit: int = 50
    ) -> Sequence[RawMessage]:
        stmt = (
            select(RawMessage)
            .where(RawMessage.group_id == group_id)
            .order_by(RawMessage.timestamp.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def count_messages_since(
        self, group_id: str, since: _dt.datetime
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(RawMessage)
            .where(RawMessage.group_id == group_id, RawMessage.timestamp >= since)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one()

    async def update_message_embedding(self, message_id: int, embedding: Any) -> None:
        stmt = (
            update(RawMessage)
            .where(RawMessage.id == message_id)
            .values(embedding=embedding)
        )
        await self._s.execute(stmt)

    # =====================================================================
    #  GroupProfile
    # =====================================================================

    async def get_or_create_group_profile(self, group_id: str) -> GroupProfile:
        stmt = select(GroupProfile).where(GroupProfile.group_id == group_id)
        result = await self._s.execute(stmt)
        profile = result.scalar_one_or_none()
        if profile is None:
            profile = GroupProfile(group_id=group_id)
            self._s.add(profile)
            await self._s.flush()
        return profile

    async def update_group_profile(
        self,
        group_id: str,
        *,
        base_prompt: str | None = None,
        source_whitelist: dict | None = None,
        learned_prompt: str | None = None,
        learned_prompt_history: dict | None = None,
        message_count_since_learn: int | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if base_prompt is not None:
            values["base_prompt"] = base_prompt
        if source_whitelist is not None:
            values["source_whitelist"] = source_whitelist
        if learned_prompt is not None:
            values["learned_prompt"] = learned_prompt
        if learned_prompt_history is not None:
            values["learned_prompt_history"] = learned_prompt_history
        if message_count_since_learn is not None:
            values["message_count_since_learn"] = message_count_since_learn
        if values:
            stmt = (
                update(GroupProfile)
                .where(GroupProfile.group_id == group_id)
                .values(**values)
            )
            await self._s.execute(stmt)

    async def increment_group_message_count(self, group_id: str) -> int:
        """Atomically increment and return the new message_count_since_learn."""
        await self.get_or_create_group_profile(group_id)
        stmt = (
            update(GroupProfile)
            .where(GroupProfile.group_id == group_id)
            .values(message_count_since_learn=GroupProfile.message_count_since_learn + 1)
            .returning(GroupProfile.message_count_since_learn)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one()

    async def list_all_groups(self) -> Sequence[GroupProfile]:
        stmt = select(GroupProfile).order_by(GroupProfile.updated_at.desc())
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def get_recently_active_group_ids(
        self,
        *,
        since: _dt.datetime,
        limit: int = 100,
    ) -> list[str]:
        stmt = (
            select(RawMessage.group_id)
            .where(RawMessage.timestamp >= since)
            .group_by(RawMessage.group_id)
            .order_by(func.max(RawMessage.timestamp).desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return [row[0] for row in result.fetchall() if row[0]]

    # =====================================================================
    #  ActiveThread
    # =====================================================================

    async def get_active_threads(
        self, group_id: str, limit: int = 10
    ) -> Sequence[ActiveThread]:
        stmt = (
            select(ActiveThread)
            .where(
                ActiveThread.group_id == group_id,
                ActiveThread.is_archived == False,  # noqa: E712
            )
            .order_by(ActiveThread.last_activity.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def create_thread(
        self, group_id: str, topic_summary: str = "", centroid: Any = None
    ) -> ActiveThread:
        thread = ActiveThread(
            group_id=group_id,
            topic_summary=topic_summary,
            centroid=centroid,
            message_count=0,
        )
        self._s.add(thread)
        await self._s.flush()
        return thread

    async def update_thread(
        self,
        thread_id: int,
        *,
        topic_summary: str | None = None,
        centroid: Any = None,
        message_count: int | None = None,
        last_activity: _dt.datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if topic_summary is not None:
            values["topic_summary"] = topic_summary
        if centroid is not None:
            values["centroid"] = centroid
        if message_count is not None:
            values["message_count"] = message_count
        if last_activity is not None:
            values["last_activity"] = last_activity
        if values:
            stmt = (
                update(ActiveThread)
                .where(ActiveThread.id == thread_id)
                .values(**values)
            )
            await self._s.execute(stmt)

    async def archive_stale_threads(
        self, group_id: str, before: _dt.datetime
    ) -> int:
        stmt = (
            update(ActiveThread)
            .where(
                ActiveThread.group_id == group_id,
                ActiveThread.is_archived == False,  # noqa: E712
                ActiveThread.last_activity < before,
            )
            .values(is_archived=True)
        )
        result = await self._s.execute(stmt)
        return result.rowcount  # type: ignore[return-value]

    async def add_message_to_thread(
        self, message_id: int, thread_id: int
    ) -> None:
        tm = ThreadMessage(message_id=message_id, thread_id=thread_id)
        self._s.add(tm)

    async def get_thread_messages(
        self, thread_id: int, limit: int = 20
    ) -> Sequence[RawMessage]:
        stmt = (
            select(RawMessage)
            .join(ThreadMessage, ThreadMessage.message_id == RawMessage.id)
            .where(ThreadMessage.thread_id == thread_id)
            .order_by(RawMessage.timestamp.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    # =====================================================================
    #  UserMemory
    # =====================================================================

    async def add_user_memory(
        self,
        group_id: str,
        user_id: str,
        fact: str,
        importance: float = 0.5,
        embedding: Any = None,
    ) -> int:
        mem = UserMemory(
            group_id=group_id,
            user_id=user_id,
            fact=fact,
            importance=importance,
            embedding=embedding,
        )
        self._s.add(mem)
        await self._s.flush()
        return mem.id

    async def search_user_memories(
        self,
        group_id: str,
        user_id: str,
        query_embedding: Any,
        top_k: int = 5,
    ) -> Sequence[UserMemory]:
        """pgvector nearest-neighbor search on user_memories."""
        stmt = (
            select(UserMemory)
            .where(
                UserMemory.group_id == group_id,
                UserMemory.user_id == user_id,
                UserMemory.embedding.isnot(None),
            )
            .order_by(UserMemory.embedding.cosine_distance(query_embedding))
            .limit(top_k)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def get_user_memories(
        self, group_id: str, user_id: str, limit: int = 20
    ) -> Sequence[UserMemory]:
        stmt = (
            select(UserMemory)
            .where(UserMemory.group_id == group_id, UserMemory.user_id == user_id)
            .order_by(UserMemory.created_at.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    # =====================================================================
    #  EmotionState
    # =====================================================================

    async def get_or_create_emotion(self, group_id: str, default_mood: str = "neutral") -> EmotionState:
        stmt = select(EmotionState).where(EmotionState.group_id == group_id)
        result = await self._s.execute(stmt)
        state = result.scalar_one_or_none()
        if state is None:
            state = EmotionState(group_id=group_id, mood=default_mood)
            self._s.add(state)
            await self._s.flush()
        return state

    async def update_emotion(
        self,
        group_id: str,
        mood: str,
        valence: float = 0.0,
        arousal: float = 0.0,
    ) -> None:
        stmt = (
            update(EmotionState)
            .where(EmotionState.group_id == group_id)
            .values(mood=mood, valence=valence, arousal=arousal)
        )
        await self._s.execute(stmt)

    # =====================================================================
    #  JargonTerm
    # =====================================================================

    async def upsert_jargon(
        self,
        group_id: str,
        term: str,
        frequency: int = 1,
        meaning: str = "",
        is_custom: bool = False,
    ) -> None:
        """Insert or increment frequency (PostgreSQL upsert)."""
        stmt = pg_insert(JargonTerm).values(
            group_id=group_id,
            term=term,
            frequency=frequency,
            meaning=meaning,
            is_custom=is_custom,
        )
        # On conflict: increment frequency, only update meaning if provided
        update_dict: dict[str, Any] = {
            "frequency": JargonTerm.frequency + frequency,
        }
        if meaning:
            update_dict["meaning"] = meaning
        stmt = stmt.on_conflict_do_update(
            index_elements=["group_id", "term"],
            set_=update_dict,
        )
        await self._s.execute(stmt)

    async def get_group_jargon(
        self, group_id: str, min_frequency: int = 1
    ) -> Sequence[JargonTerm]:
        stmt = (
            select(JargonTerm)
            .where(
                JargonTerm.group_id == group_id,
                JargonTerm.frequency >= min_frequency,
            )
            .order_by(JargonTerm.frequency.desc())
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def get_jargon_needing_meaning(
        self, group_id: str, min_frequency: int = 5, limit: int = 20
    ) -> Sequence[JargonTerm]:
        """Get terms above frequency threshold that have no meaning yet."""
        stmt = (
            select(JargonTerm)
            .where(
                JargonTerm.group_id == group_id,
                JargonTerm.frequency >= min_frequency,
                JargonTerm.meaning == "",
                JargonTerm.is_custom == False,  # noqa: E712
            )
            .order_by(JargonTerm.frequency.desc())
            .limit(limit)
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def update_jargon_meaning(self, jargon_id: int, meaning: str) -> None:
        stmt = (
            update(JargonTerm)
            .where(JargonTerm.id == jargon_id)
            .values(meaning=meaning)
        )
        await self._s.execute(stmt)

    async def delete_jargon(self, jargon_id: int) -> None:
        stmt = delete(JargonTerm).where(JargonTerm.id == jargon_id)
        await self._s.execute(stmt)

    async def add_custom_jargon(
        self, group_id: str, term: str, meaning: str
    ) -> int:
        j = JargonTerm(
            group_id=group_id,
            term=term,
            meaning=meaning,
            frequency=0,
            is_custom=True,
        )
        self._s.add(j)
        await self._s.flush()
        return j.id

    # =====================================================================
    #  BotResponse
    # =====================================================================

    async def save_bot_response(
        self,
        group_id: str,
        response_text: str,
        thread_id: int | None = None,
    ) -> int:
        r = BotResponse(
            group_id=group_id,
            thread_id=thread_id,
            response_text=response_text,
        )
        self._s.add(r)
        await self._s.flush()
        return r.id

    # =====================================================================
    #  LearningJob
    # =====================================================================

    async def create_learning_job(
        self, group_id: str, job_type: str
    ) -> int:
        job = LearningJob(group_id=group_id, job_type=job_type, status="pending")
        self._s.add(job)
        await self._s.flush()
        return job.id

    async def complete_learning_job(
        self, job_id: int, result: dict | None = None
    ) -> None:
        stmt = (
            update(LearningJob)
            .where(LearningJob.id == job_id)
            .values(
                status="completed",
                result=result,
                completed_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        await self._s.execute(stmt)

    async def fail_learning_job(self, job_id: int, error: str) -> None:
        stmt = (
            update(LearningJob)
            .where(LearningJob.id == job_id)
            .values(status="failed", result={"error": error})
        )
        await self._s.execute(stmt)

    # =====================================================================
    #  LearnedPromptReview
    # =====================================================================

    async def supersede_pending_reviews(
        self,
        group_id: str,
        prompt_type: str,
    ) -> int:
        stmt = (
            update(LearnedPromptReview)
            .where(
                LearnedPromptReview.group_id == group_id,
                LearnedPromptReview.prompt_type == prompt_type,
                LearnedPromptReview.status == "pending",
            )
            .values(
                status="superseded",
                reviewed_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        result = await self._s.execute(stmt)
        return result.rowcount or 0

    async def create_learned_prompt_review(
        self,
        *,
        group_id: str,
        prompt_type: str,
        old_value: str,
        proposed_value: str,
        change_summary: str = "",
        metadata_json: dict | None = None,
        target_tone_version_id: int | None = None,
    ) -> LearnedPromptReview:
        review = LearnedPromptReview(
            group_id=group_id,
            prompt_type=prompt_type,
            status="pending",
            old_value=old_value,
            proposed_value=proposed_value,
            change_summary=change_summary,
            metadata_json=metadata_json,
            target_tone_version_id=target_tone_version_id,
        )
        self._s.add(review)
        await self._s.flush()
        return review

    async def get_learned_prompt_review(
        self,
        review_id: int,
    ) -> LearnedPromptReview | None:
        stmt = select(LearnedPromptReview).where(LearnedPromptReview.id == review_id)
        result = await self._s.execute(stmt)
        return result.scalar_one_or_none()

    async def get_pending_learned_prompt_reviews(
        self,
        *,
        group_id: str | None = None,
        prompt_type: str | None = None,
        limit: int = 50,
    ) -> Sequence[LearnedPromptReview]:
        stmt = select(LearnedPromptReview).where(LearnedPromptReview.status == "pending")
        if group_id is not None:
            stmt = stmt.where(LearnedPromptReview.group_id == group_id)
        if prompt_type is not None:
            stmt = stmt.where(LearnedPromptReview.prompt_type == prompt_type)
        stmt = stmt.order_by(LearnedPromptReview.created_at.desc()).limit(limit)
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def get_review_history(
        self,
        group_id: str,
        *,
        prompt_type: str | None = None,
        limit: int = 50,
    ) -> Sequence[LearnedPromptReview]:
        stmt = select(LearnedPromptReview).where(LearnedPromptReview.group_id == group_id)
        if prompt_type is not None:
            stmt = stmt.where(LearnedPromptReview.prompt_type == prompt_type)
        stmt = stmt.order_by(LearnedPromptReview.created_at.desc()).limit(limit)
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def set_learned_prompt_review_status(
        self,
        review_id: int,
        *,
        status: str,
        reviewed_by: str | None = None,
        review_notes: str = "",
        activated: bool = False,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "reviewed_by": reviewed_by,
            "review_notes": review_notes,
            "reviewed_at": _dt.datetime.now(_dt.timezone.utc),
        }
        if activated:
            values["activated_at"] = _dt.datetime.now(_dt.timezone.utc)
        stmt = (
            update(LearnedPromptReview)
            .where(LearnedPromptReview.id == review_id)
            .values(**values)
        )
        result = await self._s.execute(stmt)
        return bool(result.rowcount)

    async def prune_old_learned_prompt_reviews(
        self,
        *,
        before: _dt.datetime,
        statuses: Sequence[str],
    ) -> int:
        stmt = delete(LearnedPromptReview).where(
            LearnedPromptReview.created_at < before,
            LearnedPromptReview.status.in_(list(statuses)),
        )
        result = await self._s.execute(stmt)
        return result.rowcount or 0

    async def approve_learned_prompt_review(
        self,
        review_id: int,
        *,
        reviewed_by: str | None = None,
        review_notes: str = "",
        max_group_history_versions: int = 10,
    ) -> bool:
        review = await self.get_learned_prompt_review(review_id)
        if review is None or review.status != "pending":
            return False

        if review.prompt_type == "group_persona":
            profile = await self.get_or_create_group_profile(review.group_id)
            history = profile.learned_prompt_history or []
            if profile.learned_prompt:
                history.append(
                    {
                        "prompt": profile.learned_prompt,
                        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    }
                )
                history = history[-max_group_history_versions:]
            await self.update_group_profile(
                review.group_id,
                learned_prompt=review.proposed_value,
                learned_prompt_history=history,
            )
        elif review.prompt_type == "tone_version":
            if review.target_tone_version_id is None:
                return False
            ok = await self.set_active_tone_version(review.group_id, review.target_tone_version_id)
            if not ok:
                return False
        else:
            return False

        return await self.set_learned_prompt_review_status(
            review_id,
            status="approved",
            reviewed_by=reviewed_by,
            review_notes=review_notes,
            activated=True,
        )

    async def reject_learned_prompt_review(
        self,
        review_id: int,
        *,
        reviewed_by: str | None = None,
        review_notes: str = "",
    ) -> bool:
        review = await self.get_learned_prompt_review(review_id)
        if review is None or review.status != "pending":
            return False
        return await self.set_learned_prompt_review_status(
            review_id,
            status="rejected",
            reviewed_by=reviewed_by,
            review_notes=review_notes,
            activated=False,
        )

    # =====================================================================
    #  GroupPersonaBinding & PersonaToneVersion
    # =====================================================================

    async def get_or_create_persona_binding(self, group_id: str) -> GroupPersonaBinding:
        stmt = select(GroupPersonaBinding).where(GroupPersonaBinding.group_id == group_id)
        result = await self._s.execute(stmt)
        binding = result.scalar_one_or_none()
        if binding is None:
            binding = GroupPersonaBinding(group_id=group_id)
            self._s.add(binding)
            await self._s.flush()
        return binding

    async def get_persona_binding_with_active_tone(
        self, group_id: str
    ) -> tuple[GroupPersonaBinding | None, str]:
        """Return (binding, active_tone_text). Returns (None, '') if no binding."""
        stmt = select(GroupPersonaBinding).where(GroupPersonaBinding.group_id == group_id)
        result = await self._s.execute(stmt)
        binding = result.scalar_one_or_none()
        if binding is None:
            return None, ""

        tone_text = ""
        if binding.active_version is not None:
            tone_text = binding.active_version.learned_tone
        return binding, tone_text

    async def add_new_tone_version(
        self,
        group_id: str,
        learned_tone: str,
        auto_activate: bool = True,
        is_manual: bool = False,
    ) -> PersonaToneVersion:
        """Insert a new tone version and optionally activate it."""
        # Get or create binding
        binding = await self.get_or_create_persona_binding(group_id)

        # Determine next version number
        max_stmt = (
            select(func.coalesce(func.max(PersonaToneVersion.version_num), 0))
            .where(PersonaToneVersion.group_id == group_id)
        )
        result = await self._s.execute(max_stmt)
        next_num = result.scalar_one() + 1

        version = PersonaToneVersion(
            group_id=group_id,
            version_num=next_num,
            learned_tone=learned_tone,
            is_manual=is_manual,
        )
        self._s.add(version)
        await self._s.flush()

        if auto_activate:
            binding.active_version_id = version.id
            await self._s.flush()

        return version

    async def set_active_tone_version(
        self, group_id: str, version_id: int
    ) -> bool:
        """Switch active version. Returns True if successful."""
        # Verify the version belongs to this group
        ver_stmt = select(PersonaToneVersion).where(
            PersonaToneVersion.id == version_id,
            PersonaToneVersion.group_id == group_id,
        )
        result = await self._s.execute(ver_stmt)
        version = result.scalar_one_or_none()
        if version is None:
            return False

        stmt = (
            update(GroupPersonaBinding)
            .where(GroupPersonaBinding.group_id == group_id)
            .values(active_version_id=version_id)
        )
        await self._s.execute(stmt)
        return True

    async def set_active_tone_version_by_num(
        self, group_id: str, version_num: int
    ) -> bool:
        """Switch active version by version_num. Returns True if successful."""
        ver_stmt = select(PersonaToneVersion).where(
            PersonaToneVersion.group_id == group_id,
            PersonaToneVersion.version_num == version_num,
        )
        result = await self._s.execute(ver_stmt)
        version = result.scalar_one_or_none()
        if version is None:
            return False
        return await self.set_active_tone_version(group_id, version.id)

    async def get_tone_versions(
        self, group_id: str
    ) -> Sequence[PersonaToneVersion]:
        stmt = (
            select(PersonaToneVersion)
            .where(PersonaToneVersion.group_id == group_id)
            .order_by(PersonaToneVersion.version_num.desc())
        )
        result = await self._s.execute(stmt)
        return result.scalars().all()

    async def increment_tone_message_count(self, group_id: str) -> int:
        """Atomically increment and return the new tone_message_count."""
        # Ensure binding exists
        await self.get_or_create_persona_binding(group_id)
        stmt = (
            update(GroupPersonaBinding)
            .where(GroupPersonaBinding.group_id == group_id)
            .values(tone_message_count=GroupPersonaBinding.tone_message_count + 1)
            .returning(GroupPersonaBinding.tone_message_count)
        )
        result = await self._s.execute(stmt)
        return result.scalar_one()

    async def reset_tone_message_count(self, group_id: str) -> None:
        stmt = (
            update(GroupPersonaBinding)
            .where(GroupPersonaBinding.group_id == group_id)
            .values(tone_message_count=0)
        )
        await self._s.execute(stmt)

    async def update_persona_binding(
        self,
        group_id: str,
        *,
        bound_persona_id: str | None = ...,
        is_learning_enabled: bool | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if bound_persona_id is not ...:
            values["bound_persona_id"] = bound_persona_id
        if is_learning_enabled is not None:
            values["is_learning_enabled"] = is_learning_enabled
        if values:
            stmt = (
                update(GroupPersonaBinding)
                .where(GroupPersonaBinding.group_id == group_id)
                .values(**values)
            )
            await self._s.execute(stmt)

    async def prune_old_tone_versions(
        self, group_id: str, keep_count: int = 10
    ) -> int:
        """Delete old tone versions beyond keep_count. Returns deleted count."""
        # Get all version IDs ordered by version_num desc
        stmt = (
            select(PersonaToneVersion.id)
            .where(PersonaToneVersion.group_id == group_id)
            .order_by(PersonaToneVersion.version_num.desc())
        )
        result = await self._s.execute(stmt)
        all_ids = [r[0] for r in result.fetchall()]

        if len(all_ids) <= keep_count:
            return 0

        # Get the binding to check active_version_id
        binding_stmt = select(GroupPersonaBinding).where(GroupPersonaBinding.group_id == group_id)
        binding_result = await self._s.execute(binding_stmt)
        binding = binding_result.scalar_one_or_none()
        active_id = binding.active_version_id if binding else None

        to_delete = all_ids[keep_count:]
        # Don't delete the active version
        if active_id and active_id in to_delete:
            to_delete.remove(active_id)

        if not to_delete:
            return 0

        del_stmt = delete(PersonaToneVersion).where(PersonaToneVersion.id.in_(to_delete))
        result = await self._s.execute(del_stmt)
        return result.rowcount
