import json

import pytest
from fastapi.testclient import TestClient

import shuo.server as server
from shuo.realtime.auth import clear_replay_cache


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("REALTIME_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("REALTIME_TOKEN_TTL_SECONDS", "60")
    monkeypatch.setenv("REALTIME_ALLOWED_ORIGINS", "http://localhost:3040")
    monkeypatch.setenv("REALTIME_MAX_SESSION_MINTS_PER_MIN", "30")
    monkeypatch.setenv("REALTIME_MAX_SESSIONS_PER_IP", "3")

    server._draining = False
    server._active_calls = 0
    server._session_mints_by_ip.clear()
    server._realtime_active_by_ip.clear()
    clear_replay_cache()


def test_create_realtime_session_success():
    with TestClient(server.app) as client:
        res = client.post("/v1/realtime/sessions")

    assert res.status_code == 201
    data = res.json()
    assert "token" in data
    assert "session_id" in data
    assert "expires_at" in data
    assert "ws_url" in data


def test_create_realtime_session_origin_rejected():
    with TestClient(server.app) as client:
        res = client.post("/v1/realtime/sessions", headers={"Origin": "https://evil.example"})

    assert res.status_code == 403


def test_realtime_websocket_rejects_invalid_token():
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/realtime?token=bad-token") as ws:
            message = ws.receive_json()

    assert message["type"] == "error"
    assert message["code"] == "auth_failed"


def test_realtime_websocket_accepts_valid_token(monkeypatch):
    async def fake_runner(websocket, session_id):
        await websocket.send_text(json.dumps({"type": "runner.started", "session_id": session_id}))
        await websocket.close()

    monkeypatch.setattr(server, "run_conversation_over_realtime", fake_runner)

    with TestClient(server.app) as client:
        session = client.post("/v1/realtime/sessions").json()
        token = session["token"]

        with client.websocket_connect(f"/ws/realtime?token={token}") as ws:
            message = ws.receive_json()

    assert message["type"] == "runner.started"


def test_realtime_token_single_use(monkeypatch):
    async def fake_runner(websocket, session_id):
        await websocket.close()

    monkeypatch.setattr(server, "run_conversation_over_realtime", fake_runner)

    with TestClient(server.app) as client:
        token = client.post("/v1/realtime/sessions").json()["token"]

        with client.websocket_connect(f"/ws/realtime?token={token}"):
            pass

        with client.websocket_connect(f"/ws/realtime?token={token}") as ws:
            message = ws.receive_json()

    assert message["type"] == "error"
    assert message["code"] == "auth_failed"
