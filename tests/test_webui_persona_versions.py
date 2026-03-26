from types import SimpleNamespace

from fastapi.testclient import TestClient

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.webui import api as webui_api


class FakeSession:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.state["commits"] = self.state.get("commits", 0) + 1


class FakeDatabase:
    def __init__(self, state):
        self.state = state

    def session(self):
        return FakeSession(self.state)


class FakeRepository:
    def __init__(self, session):
        self.state = session.state

    async def get_persona_binding(self, group_id):
        self.state["looked_up_group_id"] = group_id
        return self.state.get("binding")

    async def clear_all_persona_versions(self, group_id):
        self.state["cleared_group_id"] = group_id
        return self.state.get("clear_result", (0, 0))


def test_clear_persona_versions_endpoint(monkeypatch):
    import astrbot_plugin_sb_qunyou.db.repo as repo_module

    monkeypatch.setattr(repo_module, "Repository", FakeRepository)
    state = {
        "binding": SimpleNamespace(group_id="g1"),
        "clear_result": (3, 1),
        "commits": 0,
    }
    plugin = SimpleNamespace(_tone_learning_locks=set())

    app = webui_api.create_api(
        lambda: FakeDatabase(state),
        PluginConfig().webui,
        plugin_getter=lambda: plugin,
    )
    client = TestClient(app)

    response = client.delete("/api/persona-bindings/g1/versions")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "deleted_versions": 3,
        "superseded_reviews": 1,
    }
    assert state["looked_up_group_id"] == "g1"
    assert state["cleared_group_id"] == "g1"
    assert state["commits"] == 1
