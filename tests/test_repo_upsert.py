"""Repository upsert behavior tests for unique-key backed writes."""

from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from astrbot_plugin_sb_qunyou.db.repo import Repository


class DummyResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class RecordingSession:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        if self.results:
            return self.results.pop(0)
        return DummyResult()


def compile_sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


@pytest.mark.asyncio
async def test_get_or_create_group_profile_uses_insert_on_conflict_do_nothing():
    session = RecordingSession(DummyResult(), DummyResult(SimpleNamespace(group_id="g1")))
    repo = Repository(session)

    profile = await repo.get_or_create_group_profile("g1")

    assert profile.group_id == "g1"
    sql = compile_sql(session.statements[0])
    assert "INSERT INTO group_profiles" in sql
    assert "ON CONFLICT (group_id) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_increment_group_message_count_uses_atomic_upsert():
    session = RecordingSession(DummyResult(3))
    repo = Repository(session)

    count = await repo.increment_group_message_count("g1")

    assert count == 3
    sql = compile_sql(session.statements[0])
    assert "INSERT INTO group_profiles" in sql
    assert "ON CONFLICT (group_id) DO UPDATE" in sql
    assert "message_count_since_learn" in sql


@pytest.mark.asyncio
async def test_update_group_profile_allows_overriding_default_insert_fields():
    session = RecordingSession(DummyResult())
    repo = Repository(session)

    await repo.update_group_profile(
        "g1",
        learned_prompt="learned summary",
        message_count_since_learn=0,
    )

    sql = compile_sql(session.statements[0])
    assert "INSERT INTO group_profiles" in sql
    assert "learned summary" in sql
    assert "ON CONFLICT (group_id) DO UPDATE" in sql


@pytest.mark.asyncio
async def test_update_emotion_upserts_singleton_state():
    session = RecordingSession(DummyResult())
    repo = Repository(session)

    await repo.update_emotion("g1", mood="excited", valence=0.7, arousal=0.4)

    sql = compile_sql(session.statements[0])
    assert "INSERT INTO emotion_states" in sql
    assert "ON CONFLICT (group_id) DO UPDATE" in sql
    assert "excited" in sql


@pytest.mark.asyncio
async def test_add_custom_jargon_upserts_existing_term_and_returns_id():
    session = RecordingSession(DummyResult(42))
    repo = Repository(session)

    jargon_id = await repo.add_custom_jargon("g1", "黑话", "自定义释义")

    assert jargon_id == 42
    sql = compile_sql(session.statements[0])
    assert "INSERT INTO jargon_terms" in sql
    assert "ON CONFLICT (group_id, term) DO UPDATE" in sql


@pytest.mark.asyncio
async def test_update_persona_binding_allows_overriding_learning_flag():
    session = RecordingSession(DummyResult())
    repo = Repository(session)

    await repo.update_persona_binding(
        "g1",
        bound_persona_id="persona.demo",
        is_learning_enabled=False,
    )

    sql = compile_sql(session.statements[0])
    assert "INSERT INTO group_persona_bindings" in sql
    assert "persona.demo" in sql
    assert "false" in sql.lower()
    assert "ON CONFLICT (group_id) DO UPDATE" in sql


@pytest.mark.asyncio
async def test_increment_tone_message_count_uses_atomic_upsert():
    session = RecordingSession(DummyResult(5))
    repo = Repository(session)

    count = await repo.increment_tone_message_count("g1")

    assert count == 5
    sql = compile_sql(session.statements[0])
    assert "INSERT INTO group_persona_bindings" in sql
    assert "ON CONFLICT (group_id) DO UPDATE" in sql
    assert "tone_message_count" in sql