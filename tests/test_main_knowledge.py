import asyncio
from types import MethodType

import pytest

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.main import QunyouPlugin


class FakeKnowledge:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def insert(self, group_id: str, text: str) -> bool:
        self.calls.append((group_id, text))
        return True


class DummyPlugin:
    def __init__(self, cooldown: float = 0.05):
        cfg = PluginConfig()
        cfg.knowledge.ingestion_cooldown = cooldown
        self.plugin_config = cfg
        self.knowledge = FakeKnowledge()
        self._ingestion_buffer: dict[str, list[str]] = {}
        self._ingestion_tasks: dict[str, asyncio.Task] = {}
        self.background_tasks: set[asyncio.Task] = set()

        self._flush_knowledge_buffer = MethodType(QunyouPlugin._flush_knowledge_buffer, self)
        self._schedule_knowledge_flush = MethodType(QunyouPlugin._schedule_knowledge_flush, self)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        self.background_tasks.discard(task)
        if not task.cancelled() and task.exception():
            raise task.exception()


@pytest.mark.asyncio
async def test_knowledge_flush_waits_for_cooldown():
    plugin = DummyPlugin(cooldown=0.05)
    plugin._ingestion_buffer["g1"] = ["msg1", "msg2"]

    plugin._schedule_knowledge_flush("g1")
    await asyncio.sleep(0.02)
    assert plugin.knowledge.calls == []

    await asyncio.sleep(0.06)
    assert plugin.knowledge.calls == [("g1", "msg1\n\nmsg2")]


@pytest.mark.asyncio
async def test_knowledge_flush_resets_cooldown_on_new_messages():
    plugin = DummyPlugin(cooldown=0.05)
    plugin._ingestion_buffer["g1"] = ["msg1"]

    plugin._schedule_knowledge_flush("g1")
    await asyncio.sleep(0.03)

    plugin._ingestion_buffer.setdefault("g1", []).append("msg2")
    plugin._schedule_knowledge_flush("g1")
    await asyncio.sleep(0.03)
    assert plugin.knowledge.calls == []

    await asyncio.sleep(0.04)
    assert plugin.knowledge.calls == [("g1", "msg1\n\nmsg2")]


@pytest.mark.asyncio
async def test_force_knowledge_flush_bypasses_cooldown():
    plugin = DummyPlugin(cooldown=5.0)
    plugin._ingestion_buffer["g1"] = ["msg1", "msg2"]

    plugin._schedule_knowledge_flush("g1")
    plugin._schedule_knowledge_flush("g1", force=True)
    await asyncio.sleep(0.02)

    assert plugin.knowledge.calls == [("g1", "msg1\n\nmsg2")]
