from types import SimpleNamespace

import pytest

from astrbot_plugin_sb_qunyou.config import PluginConfig, TopicConfig
from astrbot_plugin_sb_qunyou.pipeline.topic_router import TopicThreadRouter
from astrbot_plugin_sb_qunyou.services.llm_adapter import LLMAdapter


class FakeProviderLLM:
    def __init__(self, name: str):
        self.name = name
        self.calls = []

    async def text_chat(self, prompt: str, system_prompt: str = "", **kwargs):
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(completion_text=f"{self.name}:{prompt}")

    async def get_embeddings(self, texts: list[str]):
        return [[float(len(texts[0]))]]


class FakeProvider:
    def __init__(self, name: str):
        self.llm = FakeProviderLLM(name)

    async def get_embeddings(self, texts: list[str]):
        return await self.llm.get_embeddings(texts)


class FakeContext:
    def __init__(self, providers: dict[str, FakeProvider], fallback_order: list[FakeProvider] | None = None):
        self.providers = providers
        self.fallback_order = fallback_order or list(providers.values())

    def get_provider_by_id(self, provider_id: str):
        return self.providers.get(provider_id)

    def get_all_providers(self):
        return self.fallback_order


class FakePromptService:
    async def get_prompt(self, key: str):
        assert key == "THREAD_SUMMARY"
        return "主题摘要：\n{messages}"


class FakeSession:
    def __init__(self, state: dict):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.state["commits"] = self.state.get("commits", 0) + 1


class FakeDatabase:
    def __init__(self, state: dict):
        self.state = state

    def session(self):
        return FakeSession(self.state)


class FakeRepository:
    def __init__(self, session: FakeSession):
        self.state = session.state

    async def get_thread_messages(self, thread_id: int, limit: int):
        return self.state["messages"]

    async def update_thread(self, thread_id: int, **kwargs):
        self.state.setdefault("updates", []).append((thread_id, kwargs))


@pytest.mark.asyncio
async def test_main_chat_does_not_fallback_to_fast_provider():
    fast_provider = FakeProvider("fast")
    context = FakeContext({"fast-id": fast_provider})
    config = PluginConfig(fast_llm_provider_id="fast-id")
    adapter = LLMAdapter(context, config)

    result = await adapter.main_chat("hello")

    assert result == ""
    assert fast_provider.llm.calls == []


@pytest.mark.asyncio
async def test_fast_chat_uses_configured_provider_only():
    fast_provider = FakeProvider("fast")
    fallback_provider = FakeProvider("fallback")
    context = FakeContext(
        {"fast-id": fast_provider, "fallback-id": fallback_provider},
        fallback_order=[fallback_provider, fast_provider],
    )
    config = PluginConfig(fast_llm_provider_id="fast-id")
    adapter = LLMAdapter(context, config)

    result = await adapter.fast_chat("hello")

    assert result == "fast:hello"
    assert len(fast_provider.llm.calls) == 1
    assert fallback_provider.llm.calls == []


@pytest.mark.asyncio
async def test_get_embedding_requires_configured_embedding_provider():
    embedding_provider = FakeProvider("embed")
    context = FakeContext({"embed-id": embedding_provider}, fallback_order=[embedding_provider])
    adapter = LLMAdapter(context, PluginConfig())

    result = await adapter.get_embedding("hello")

    assert result is None


@pytest.mark.asyncio
async def test_topic_summary_uses_topic_provider_override(monkeypatch):
    state = {
        "messages": [
            SimpleNamespace(sender_name="Alice", sender_id="u1", text="今天聊原神卡池"),
            SimpleNamespace(sender_name="Bob", sender_id="u2", text="然后又聊队伍搭配"),
        ]
    }
    db = FakeDatabase(state)

    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    monkeypatch.setattr(repo_module, "Repository", FakeRepository)

    class FakeLLM:
        def __init__(self):
            self.fast_calls = []
            self.chat_calls = []

        async def fast_chat(self, prompt: str):
            self.fast_calls.append(prompt)
            return "fast-summary"

        async def chat_completion(self, prompt: str, **kwargs):
            self.chat_calls.append({"prompt": prompt, **kwargs})
            return "override-summary"

    llm = FakeLLM()
    plugin = SimpleNamespace(prompt_service=FakePromptService())
    router = TopicThreadRouter(
        TopicConfig(fast_model_provider_id="topic-fast"),
        llm,
        plugin,
    )

    await router._update_thread_summary(7, db)

    assert llm.fast_calls == []
    assert llm.chat_calls == [
        {
            "prompt": "主题摘要：\n[Alice]: 今天聊原神卡池\n[Bob]: 然后又聊队伍搭配",
            "provider_id": "topic-fast",
            "allow_fallback": False,
        }
    ]
    assert state["updates"] == [(7, {"topic_summary": "override-summary"})]