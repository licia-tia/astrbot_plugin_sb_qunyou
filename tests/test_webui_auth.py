from fastapi.testclient import TestClient

from astrbot_plugin_sb_qunyou.config import PluginConfig
from astrbot_plugin_sb_qunyou.webui import api as webui_api


def test_exchange_token_accepts_json_body():
    app = webui_api.create_api(lambda: None, PluginConfig().webui)
    client = TestClient(app)

    web_token = webui_api.webui_token_generator()
    response = client.post("/api/auth/exchange", json={"token": web_token})

    assert response.status_code == 200
    data = response.json()
    assert data["session_token"]
    assert data["expires_at"]


def test_login_accepts_json_body_after_exchange():
    app = webui_api.create_api(lambda: None, PluginConfig().webui)
    client = TestClient(app)

    web_token = webui_api.webui_token_generator()
    exchange_response = client.post("/api/auth/exchange", json={"token": web_token})
    session_token = exchange_response.json()["session_token"]

    login_response = client.post("/api/auth/login", json={"token": session_token})

    assert login_response.status_code == 200
    assert login_response.json() == {"logged_in": True}


def test_auth_status_accepts_request_without_422():
    app = webui_api.create_api(lambda: None, PluginConfig().webui)
    client = TestClient(app)

    response = client.get("/api/auth/status")

    assert response.status_code == 200
    assert response.json() == {"logged_in": True}