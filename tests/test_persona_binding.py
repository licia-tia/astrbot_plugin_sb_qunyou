from types import SimpleNamespace

import pytest

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.services.persona_binding import PersonaBindingService


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None


class FakeDatabase:
    def __init__(self, state):
        self.state = state

    def session(self):
        return FakeSession(self.state)


class FakeRepository:
    def __init__(self, session):
        self.state = session.state

    async def get_persona_binding_with_active_persona(self, group_id):
        return self.state.get("binding"), self.state.get("active_text", "")

    async def get_persona_binding(self, group_id):
        return self.state.get("binding")


class FakeInjector:
    def __init__(self, persona_manager):
        self.persona_manager = persona_manager

    def get(self, _cls):
        return self.persona_manager


class FakeContext:
    def __init__(self, persona_manager):
        self.persona_manager = persona_manager

    def get_injector(self):
        return FakeInjector(self.persona_manager)


def test_extract_persona_slot_content_prefers_inner_slot_text():
    raw_prompt = (
        "你是一个友善的助手。\n\n"
        "<qunyou_persona_slot>\n"
        "这里是真正的群人格内容。\n"
        "</qunyou_persona_slot>\n\n"
        "请遵守平台规则。"
    )

    extracted = PersonaBindingService.extract_persona_slot_content(raw_prompt)

    assert extracted == "这里是真正的群人格内容。"


@pytest.mark.asyncio
async def test_resolve_effective_persona_prompt_prefers_local_active_version(monkeypatch):
    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    monkeypatch.setattr(repo_module, "Repository", FakeRepository)
    service = PersonaBindingService(PluginConfig(), SimpleNamespace(), SimpleNamespace())
    state = {
        "binding": SimpleNamespace(
            bound_persona_id="astrbot-default",
            base_persona_prompt="你是本群的默认助手。",
            active_version_id=7,
        ),
        "active_text": "你是本群的升级版助手，说话更贴近群内氛围。",
    }

    prompt, source = await service.resolve_effective_persona_prompt("g1", FakeDatabase(state))

    assert prompt == "你是本群的升级版助手，说话更贴近群内氛围。"
    assert source == "active_version"


@pytest.mark.asyncio
async def test_list_persona_catalog_reads_astrbot_personas_from_manager_list():
    persona_manager = SimpleNamespace(
        personas=[
            SimpleNamespace(
                persona_id="idol",
                display_name="偶像人格",
                prompt="<qunyou_persona_slot>热情、活泼、会接梗</qunyou_persona_slot>",
            ),
            SimpleNamespace(
                persona_id="calm",
                name="冷静人格",
                system_prompt="保持克制，先总结再表达观点。",
            ),
        ]
    )
    service = PersonaBindingService(PluginConfig(), SimpleNamespace(), FakeContext(persona_manager))

    catalog = await service.list_persona_catalog()

    assert [item["persona_id"] for item in catalog] == ["calm", "idol"]
    assert catalog[0]["display_name"] == "冷静人格"
    assert catalog[0]["effective_prompt"] == "保持克制，先总结再表达观点。"
    assert catalog[1]["display_name"] == "偶像人格"
    assert catalog[1]["effective_prompt"] == "热情、活泼、会接梗"
    assert catalog[1]["has_persona_slot"] is True


@pytest.mark.asyncio
async def test_get_persona_prompt_by_id_supports_sync_get_persona():
    persona_manager = SimpleNamespace(
        get_persona=lambda persona_id: SimpleNamespace(
            persona_id=persona_id,
            prompt="你是一个稳定的人格。",
        )
    )
    service = PersonaBindingService(PluginConfig(), SimpleNamespace(), FakeContext(persona_manager))

    prompt = await service.get_persona_prompt_by_id("steady", service._context)

    assert prompt == "你是一个稳定的人格。"