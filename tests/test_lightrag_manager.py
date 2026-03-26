import pytest

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.services.knowledge import lightrag_manager as manager_mod


class FakeEmbeddingFunc:
    def __init__(self, *, embedding_dim, func, max_token_size=None):
        self.embedding_dim = embedding_dim
        self.max_token_size = max_token_size
        self.func = func


def fake_wrap_embedding_func_with_attrs(
    *, embedding_dim, max_token_size=None, model_name=None
):
    def _decorator(func):
        class WrappedEmbeddingFunc:
            def __init__(self):
                self.embedding_dim = embedding_dim
                self.max_token_size = max_token_size
                self.model_name = model_name
                self.func = func

            async def __call__(self, texts):
                return await self.func(texts)

        return WrappedEmbeddingFunc()

    return _decorator


class FakeLightRAG:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.init_calls: list[str] = []

    async def initialize_storages(self):
        self.init_calls.append("storages")

    async def initialize_pipeline_status(self):
        self.init_calls.append("pipeline")


class FakeLLM:
    def __init__(self):
        self.chat_calls: list[dict] = []

    async def get_embedding(self, text: str):
        return [float(len(text)), 1.0]

    async def chat_completion(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        provider_id=None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        allow_fallback: bool = True,
        response_format=None,
    ) -> str:
        self.chat_calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "provider_id": provider_id,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "allow_fallback": allow_fallback,
            }
        )
        return "ok"


@pytest.mark.asyncio
async def test_get_instance_injects_embedding_and_llm(monkeypatch):
    monkeypatch.setattr(manager_mod, "HAS_LIGHTRAG", True)
    monkeypatch.setattr(manager_mod, "_LightRAG", FakeLightRAG)
    monkeypatch.setattr(manager_mod, "EmbeddingFunc", FakeEmbeddingFunc)
    monkeypatch.setattr(
        manager_mod,
        "wrap_embedding_func_with_attrs",
        fake_wrap_embedding_func_with_attrs,
    )

    cfg = PluginConfig()
    cfg.embedding_dim = 1024
    cfg.embedding_provider_id = "embed-provider"
    cfg.main_llm_provider_id = "main-provider"
    llm = FakeLLM()
    mgr = manager_mod.LightRAGKnowledgeManager(cfg, llm)

    instance = await mgr._get_instance("group/1")

    assert instance is not None
    assert instance.init_calls == ["storages", "pipeline"]
    assert instance.kwargs["working_dir"].endswith("group_1")
    assert hasattr(instance.kwargs["embedding_func"], "func")
    assert instance.kwargs["embedding_func"].embedding_dim == 1024
    assert instance.kwargs["embedding_func"].max_token_size == 8192
    assert instance.kwargs["embedding_func"].model_name == "embed-provider"

    vectors = await instance.kwargs["embedding_func"].func(["hello", "world"])
    assert vectors.shape == (2, 2)

    result = await instance.kwargs["llm_model_func"](
        "current prompt",
        system_prompt="system prompt",
        history_messages=[{"role": "user", "content": "older message"}],
        keyword_extraction=True,
    )
    assert result == "ok"
    assert llm.chat_calls[0]["provider_id"] == "main-provider"
    assert llm.chat_calls[0]["temperature"] == 0.0
    assert "[History]" in llm.chat_calls[0]["prompt"]
    assert "older message" in llm.chat_calls[0]["prompt"]


@pytest.mark.asyncio
async def test_get_instance_initializes_supported_hooks(monkeypatch):
    class StorageOnlyLightRAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.init_calls: list[str] = []

        async def initialize_storages(self):
            self.init_calls.append("storages")

    monkeypatch.setattr(manager_mod, "HAS_LIGHTRAG", True)
    monkeypatch.setattr(manager_mod, "_LightRAG", StorageOnlyLightRAG)
    monkeypatch.setattr(manager_mod, "EmbeddingFunc", FakeEmbeddingFunc)
    monkeypatch.setattr(
        manager_mod,
        "wrap_embedding_func_with_attrs",
        fake_wrap_embedding_func_with_attrs,
    )

    mgr = manager_mod.LightRAGKnowledgeManager(PluginConfig(), FakeLLM())

    instance = await mgr._get_instance("g1")

    assert instance is not None
    assert instance.init_calls == ["storages"]


@pytest.mark.asyncio
async def test_embedding_wrapper_fallback_provides_func_attr(monkeypatch):
    monkeypatch.setattr(manager_mod, "HAS_LIGHTRAG", True)
    monkeypatch.setattr(manager_mod, "EmbeddingFunc", None)
    monkeypatch.setattr(manager_mod, "wrap_embedding_func_with_attrs", None)

    mgr = manager_mod.LightRAGKnowledgeManager(PluginConfig(), FakeLLM())
    embedding_func = mgr._build_embedding_func()

    assert hasattr(embedding_func, "func")
    assert embedding_func.embedding_dim == 1024
    assert embedding_func.model_name == "astrbot-embedding"
