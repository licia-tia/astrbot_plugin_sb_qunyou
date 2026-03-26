from types import SimpleNamespace

import pytest

from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import ProviderRequest

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.services.hook_handler import HookHandler, _rewrite_system_prompt


class FakeEvent:
    def get_group_id(self):
        return "g1"

    def get_sender_id(self):
        return "u1"

    def get_message_str(self):
        return "hello"


class FakePromptService:
    async def get_prompt(self, key):
        mapping = {
            "INJECTION_JARGON": "<jargon_hints trust=\"low\">\n{hints}\n</jargon_hints>",
        }
        return mapping[key]


class FakeContextBuilder:
    async def build_persona_injection(self, persona_prompt):
        return f"<group_persona>\n{persona_prompt}\n</group_persona>"

    async def build_emotion_injection(self, mood):
        return f"<emotion>{mood}</emotion>"

    async def build_memory_injection(self, user_id, facts):
        memory_lines = "\n".join(f"- {fact}" for fact in facts)
        return f"<user_memories user=\"{user_id}\" trust=\"medium\">\n{memory_lines}\n</user_memories>"


class FakeSpeakerMemory:
    async def retrieve_relevant(self, group_id, user_id, text, db):
        return ["喜欢测试", "经常讨论注入链路"]


class FakeKnowledge:
    async def query(self, group_id, text, **kwargs):
        return "命中知识：这里是 RAG 上下文"


# -------------------------------------------------------------------------- #
#  Unit tests for _rewrite_system_prompt
# -------------------------------------------------------------------------- #

def test_rewrite_single_slot():
    original = (
        "你是一个助手。\n\n"
        "<qunyou_persona_slot>\n默认人格\n</qunyou_persona_slot>\n\n"
        "平台规则..."
    )
    combined = "你是本群的专属游戏助手，回复时保持活泼和亲切。"

    rewritten, ok = _rewrite_system_prompt(original, combined)

    assert ok is True
    assert "<qunyou_persona_slot>" not in rewritten
    assert "专属游戏助手" in rewritten
    assert "活泼" in rewritten
    assert "平台规则" in rewritten  # untouched


def test_rewrite_no_slot_returns_false():
    original = "原始 system prompt 内容，不含插槽标签"
    rewritten, ok = _rewrite_system_prompt(original, "填充内容")
    assert ok is False
    assert rewritten == original  # unchanged


# -------------------------------------------------------------------------- #
#  Integration test: handle() with slot rewrite
# -------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_handle_normalizes_extra_parts_and_appends_text_part():
    plugin = SimpleNamespace(
        db=object(),
        context_builder=FakeContextBuilder(),
        prompt_service=FakePromptService(),
    )
    handler = HookHandler(PluginConfig(), plugin)

    async def empty_fetch(*args, **kwargs):
        return ""

    async def fetch_thread(*args, **kwargs):
        return "<thread_context>recent topic</thread_context>"

    async def fetch_memories(*args, **kwargs):
        return "- likes tests"

    async def fetch_jargon(*args, **kwargs):
        return "「yyds」= 永远的神"

    handler._fetch_persona = empty_fetch
    handler._fetch_emotion = empty_fetch
    handler._fetch_thread_context = fetch_thread
    handler._fetch_memories = fetch_memories
    handler._fetch_knowledge = empty_fetch
    handler._fetch_jargon = fetch_jargon
    handler._fetch_persona_binding = empty_fetch

    req = ProviderRequest(
        prompt="user message",
        extra_user_content_parts=["legacy string part"],
    )

    await handler.handle(FakeEvent(), req)

    assert all(isinstance(part, TextPart) for part in req.extra_user_content_parts)
    assert req.extra_user_content_parts[0].text == "legacy string part"
    assert "<thread_context>recent topic</thread_context>" in req.extra_user_content_parts[1].text
    assert "- likes tests" in req.extra_user_content_parts[1].text
    assert "「yyds」= 永远的神" in req.extra_user_content_parts[1].text

    assembled = await req.assemble_context()
    assert assembled["role"] == "user"
    assert isinstance(assembled["content"], list)
    assert any(block["type"] == "text" and block["text"] == "legacy string part" for block in assembled["content"])


@pytest.mark.asyncio
async def test_handle_slot_rewrite():
    """Group-local persona replaces the slot and preserves the surrounding system prompt."""
    plugin = SimpleNamespace(db=object(), context_builder=FakeContextBuilder())
    handler = HookHandler(PluginConfig(), plugin)

    async def empty_fetch(*args, **kwargs):
        return ""

    async def pb_fetch(*args, **kwargs):
        return "你是本群的专属游戏助手，回复时保持活泼热情。"

    handler._fetch_persona = empty_fetch
    handler._fetch_emotion = empty_fetch
    handler._fetch_thread_context = empty_fetch
    handler._fetch_memories = empty_fetch
    handler._fetch_knowledge = empty_fetch
    handler._fetch_jargon = empty_fetch
    handler._fetch_persona_binding = pb_fetch

    original_sp = (
        "你是一个友善的助手。\n\n"
        "<qunyou_persona_slot>\n默认人格\n</qunyou_persona_slot>\n\n"
        "平台安全规则..."
    )
    req = ProviderRequest(
        prompt="user message",
        system_prompt=original_sp,
        extra_user_content_parts=["legacy string part"],
    )

    await handler.handle(FakeEvent(), req)

    # Single slot replaced with the resolved group-local persona text
    assert "<qunyou_persona_slot>" not in req.system_prompt
    assert "本群的专属游戏助手" in req.system_prompt
    assert "活泼热情" in req.system_prompt
    # Non-slot content preserved
    assert "平台安全规则" in req.system_prompt
    assert "友善的助手" in req.system_prompt
    assert all(isinstance(part, TextPart) for part in req.extra_user_content_parts)
    assert req.extra_user_content_parts[0].text == "legacy string part"


@pytest.mark.asyncio
async def test_handle_injects_emotion_without_slot_rewrite():
    plugin = SimpleNamespace(db=object(), context_builder=FakeContextBuilder())
    handler = HookHandler(PluginConfig(), plugin)

    async def empty_fetch(*args, **kwargs):
        return ""

    async def fetch_emotion(*args, **kwargs):
        return "happy"

    handler._fetch_persona = empty_fetch
    handler._fetch_emotion = fetch_emotion
    handler._fetch_thread_context = empty_fetch
    handler._fetch_memories = empty_fetch
    handler._fetch_knowledge = empty_fetch
    handler._fetch_jargon = empty_fetch
    handler._fetch_persona_binding = empty_fetch

    req = ProviderRequest(
        prompt="user message",
        system_prompt="原始 system prompt，不包含插槽",
        extra_user_content_parts=[],
    )

    await handler.handle(FakeEvent(), req)

    assert req.system_prompt == "原始 system prompt，不包含插槽"
    assert any(part.text == "<emotion>happy</emotion>" for part in req.extra_user_content_parts)


@pytest.mark.asyncio
async def test_handle_records_runtime_slot_status():
    plugin = SimpleNamespace(db=object(), context_builder=FakeContextBuilder(), _prompt_slot_status={})
    handler = HookHandler(PluginConfig(), plugin)

    async def empty_fetch(*args, **kwargs):
        return ""

    handler._fetch_persona = empty_fetch
    handler._fetch_emotion = empty_fetch
    handler._fetch_thread_context = empty_fetch
    handler._fetch_memories = empty_fetch
    handler._fetch_knowledge = empty_fetch
    handler._fetch_jargon = empty_fetch
    handler._fetch_persona_binding = empty_fetch

    req = ProviderRequest(
        prompt="user message",
        system_prompt=(
            "你是一个友善的助手。\n\n"
            "<qunyou_persona_slot>\n默认人格\n</qunyou_persona_slot>"
        ),
        extra_user_content_parts=[],
    )

    await handler.handle(FakeEvent(), req)

    status = plugin._prompt_slot_status["g1"]
    assert status["has_persona_slot"] is True
    assert status["system_prompt_length"] == len(req.system_prompt)


@pytest.mark.asyncio
async def test_fetch_memories_and_knowledge_print_debug(monkeypatch):
    printed: list[str] = []

    def fake_print(*args, **kwargs):
        printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", fake_print)

    config = PluginConfig()
    config.debug.enabled = True
    plugin = SimpleNamespace(
        speaker_memory=FakeSpeakerMemory(),
        knowledge=FakeKnowledge(),
    )
    handler = HookHandler(config, plugin)
    handler._get_cache = lambda: None

    memories = await handler._fetch_memories("g1", "u1", "我最近在做测试", object())
    knowledge = await handler._fetch_knowledge("g1", "给我看看 RAG")

    assert "喜欢测试" in memories
    assert knowledge == "命中知识：这里是 RAG 上下文"
    assert any("Memory Search" in message and "喜欢测试" in message for message in printed)
    assert any("RAG Search" in message and "命中知识" in message for message in printed)
