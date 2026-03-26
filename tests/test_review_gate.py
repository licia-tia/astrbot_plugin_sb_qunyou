"""Review-gate behavior tests for learned prompt changes."""

from types import SimpleNamespace

import pytest

from astrbot_plugin_sb_qunyou.config import PluginConfig, ReviewGateConfig
from astrbot_plugin_sb_qunyou.prompts import templates as prompt_templates
from astrbot_plugin_sb_qunyou.services.group_persona import GroupPersonaService
from astrbot_plugin_sb_qunyou.services.persona_binding import PersonaBindingService


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.state["commits"] += 1


class FakeDatabase:
    def __init__(self, state):
        self.state = state

    def session(self):
        return FakeSession(self.state)


class FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.last_prompt = None

    async def main_chat(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


class FakePromptService:
    async def get_prompt(self, key: str):
        if key == "GROUP_PERSONA_LEARN":
            return "请根据以下消息总结群画像：\n{messages}"
        if key == "COMBINED_LEARNING_PROMPT":
            return prompt_templates.COMBINED_LEARNING_PROMPT
        return None


class FakeGroupPersonaRepository:
    def __init__(self, session):
        self.state = session.state

    async def create_learning_job(self, group_id, job_type):
        self.state["job_type"] = job_type
        return 1

    async def get_recent_messages(self, group_id, limit):
        return self.state["messages"]

    async def complete_learning_job(self, job_id, result):
        self.state["job_result"] = result

    async def fail_learning_job(self, job_id, error):
        self.state["job_error"] = error

    async def get_or_create_group_profile(self, group_id):
        return self.state["profile"]

    async def update_group_profile(self, group_id, **kwargs):
        self.state.setdefault("profile_updates", []).append(kwargs)

    async def supersede_pending_reviews(self, group_id, prompt_type):
        self.state["superseded"] = (group_id, prompt_type)
        return 0

    async def create_learned_prompt_review(self, **kwargs):
        self.state["created_review"] = kwargs
        return SimpleNamespace(id=9)


class FakePersonaBindingRepository:
    def __init__(self, session):
        self.state = session.state

    async def get_persona_binding_with_active_persona(self, group_id):
        return self.state["binding"], self.state["current_tone"]

    async def get_or_create_persona_binding(self, group_id):
        return self.state["binding"]

    async def create_learning_job(self, group_id, job_type):
        self.state["job_type"] = job_type
        return 2

    async def get_recent_messages(self, group_id, limit):
        return self.state["messages"]

    async def add_new_persona_version(self, group_id, persona_prompt, auto_activate, is_manual=False):
        self.state["persona_version_args"] = {
            "group_id": group_id,
            "persona_prompt": persona_prompt,
            "auto_activate": auto_activate,
        }
        return SimpleNamespace(id=11, version_num=3)

    async def supersede_pending_reviews(self, group_id, prompt_type):
        self.state["superseded"] = (group_id, prompt_type)
        return 0

    async def create_learned_prompt_review(self, **kwargs):
        self.state["created_review"] = kwargs
        return SimpleNamespace(id=12)

    async def reset_persona_message_count(self, group_id):
        self.state["persona_reset"] = group_id

    async def prune_old_persona_versions(self, group_id, keep_count):
        self.state["prune_keep_count"] = keep_count
        return 0

    async def complete_learning_job(self, job_id, result):
        self.state["job_result"] = result

    async def fail_learning_job(self, job_id, error):
        self.state["job_error"] = error


def make_messages(count: int):
    return [
        SimpleNamespace(sender_name=f"user{i}", sender_id=f"u{i}", text=f"message {i}")
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_group_persona_learning_creates_pending_review(monkeypatch):
    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    state = {
        "messages": make_messages(25),
        "profile": SimpleNamespace(learned_prompt="old persona", learned_prompt_history=[]),
        "commits": 0,
    }
    monkeypatch.setattr(repo_module, "Repository", FakeGroupPersonaRepository)

    cfg = PluginConfig(
        review_gate=ReviewGateConfig(enabled_for_group_persona=True),
    )
    service = GroupPersonaService(
        cfg,
        FakeLLM("new persona summary"),
        plugin=SimpleNamespace(prompt_service=FakePromptService()),
    )
    await service.run_batch_learning("g1", FakeDatabase(state))

    assert state["created_review"]["prompt_type"] == "group_persona"
    assert state["created_review"]["old_value"] == "old persona"
    assert state["created_review"]["proposed_value"] == "new persona summary"
    assert any(update == {"message_count_since_learn": 0} for update in state["profile_updates"])
    assert state["job_result"]["review_required"] is True
    assert state["job_result"]["review_id"] == 9


@pytest.mark.asyncio
async def test_persona_learning_creates_pending_review_and_disables_auto_activate(monkeypatch):
    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    state = {
        "messages": make_messages(25),
        "binding": SimpleNamespace(is_learning_enabled=True),
        "current_tone": "old tone",
        "commits": 0,
    }
    monkeypatch.setattr(repo_module, "Repository", FakePersonaBindingRepository)

    cfg = PluginConfig(
        review_gate=ReviewGateConfig(enabled_for_tone=True),
    )
    import types
    import types
    service = PersonaBindingService(cfg, FakeLLM("new learned tone"), types.SimpleNamespace())
    await service.run_combined_learning("g2", FakeDatabase(state))

    assert state["persona_version_args"]["auto_activate"] is False
    assert state["created_review"]["prompt_type"] == "persona_version"
    assert state["created_review"]["old_value"] == "old tone"
    assert state["created_review"]["proposed_value"] == "new learned tone"
    assert state["created_review"]["target_persona_version_id"] == 11
    assert state["job_result"]["review_required"] is True
    assert state["job_result"]["review_id"] == 12


@pytest.mark.asyncio
async def test_persona_learning_supports_empty_custom_template_and_low_threshold(monkeypatch):
    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    state = {
        "messages": make_messages(10),
        "binding": SimpleNamespace(is_learning_enabled=True),
        "current_tone": "old tone",
        "commits": 0,
    }
    monkeypatch.setattr(repo_module, "Repository", FakePersonaBindingRepository)

    cfg = PluginConfig()
    cfg.group_persona.batch_learning_threshold = 10

    llm = FakeLLM("new learned persona")
    plugin = SimpleNamespace(prompt_service=FakePromptService())
    service = PersonaBindingService(cfg, llm, SimpleNamespace(), plugin=plugin)

    await service.run_combined_learning("g3", FakeDatabase(state))

    assert state["persona_version_args"]["persona_prompt"] == "new learned persona"
    assert "old tone" in llm.last_prompt
    assert state["job_result"]["auto_activated"] is True
