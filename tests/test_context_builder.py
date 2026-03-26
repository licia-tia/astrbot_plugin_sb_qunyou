"""ContextBuilder 单元测试。"""

import pytest

from astrbot_plugin_sb_qunyou.pipeline.context_builder import ContextBuilder


class FakePromptService:
    async def get_prompt(self, key: str) -> str:
        mapping = {
            "INJECTION_GROUP_PERSONA": "<group_persona>\n{persona}\n</group_persona>",
            "INJECTION_EMOTION": "<emotion_state>{mood}</emotion_state>",
            "INJECTION_THREAD_CONTEXT": "<thread_context topic=\"{topic}\">\n{messages}\n</thread_context>",
            "INJECTION_USER_MEMORIES": "<user_memories user=\"{user_id}\" trust=\"medium\">\n{memories}\n</user_memories>",
            "INJECTION_JARGON": "<jargon_hints trust=\"low\">\n{hints}\n</jargon_hints>",
        }
        return mapping[key]


@pytest.fixture
def builder():
    return ContextBuilder(FakePromptService())


@pytest.mark.asyncio
async def test_persona_injection_non_empty(builder):
    result = await builder.build_persona_injection("这是一个游戏群")
    assert "<group_persona>" in result
    assert "这是一个游戏群" in result
    assert "</group_persona>" in result


@pytest.mark.asyncio
async def test_persona_injection_empty_returns_empty(builder):
    assert await builder.build_persona_injection("") == ""


@pytest.mark.asyncio
async def test_emotion_injection_non_neutral(builder):
    result = await builder.build_emotion_injection("happy")
    assert "<emotion_state>" in result
    assert "happy" in result


@pytest.mark.asyncio
async def test_emotion_injection_neutral_returns_empty(builder):
    assert await builder.build_emotion_injection("neutral") == ""


@pytest.mark.asyncio
async def test_emotion_injection_empty_returns_empty(builder):
    assert await builder.build_emotion_injection("") == ""


@pytest.mark.asyncio
async def test_thread_injection_with_messages(builder):
    msgs = [
        {"sender": "Alice", "text": "hello"},
        {"sender": "Bob", "text": "hi there"},
    ]
    result = await builder.build_thread_injection("游戏讨论", msgs)
    assert "<thread_context" in result
    assert "游戏讨论" in result
    assert "[Alice]: hello" in result
    assert "[Bob]: hi there" in result


@pytest.mark.asyncio
async def test_thread_injection_empty_messages(builder):
    assert await builder.build_thread_injection("topic", []) == ""


@pytest.mark.asyncio
async def test_thread_injection_no_topic_defaults(builder):
    result = await builder.build_thread_injection("", [{"sender": "A", "text": "x"}])
    assert 'topic="ongoing"' in result


@pytest.mark.asyncio
async def test_memory_injection_with_facts(builder):
    facts = ["喜欢原神", "是大学生"]
    result = await builder.build_memory_injection("user123", facts)
    assert "<user_memories" in result
    assert "user123" in result
    assert "- 喜欢原神" in result
    assert "- 是大学生" in result


@pytest.mark.asyncio
async def test_memory_injection_empty_facts(builder):
    assert await builder.build_memory_injection("u1", []) == ""


@pytest.mark.asyncio
async def test_jargon_injection_with_matches(builder):
    matches = [("yyds", "永远的神"), ("awsl", "啊我死了")]
    result = await builder.build_jargon_injection(matches)
    assert "<jargon_hints" in result
    assert "「yyds」= 永远的神" in result
    assert "「awsl」= 啊我死了" in result


@pytest.mark.asyncio
async def test_jargon_injection_empty_matches(builder):
    assert await builder.build_jargon_injection([]) == ""
