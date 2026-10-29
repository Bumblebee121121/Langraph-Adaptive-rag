import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sse_starlette.sse import AppStatus

from app.config.settings import clear_settings_cache
from app.main import create_app


@pytest.fixture
def memory_app(monkeypatch, tmp_path):
    path = tmp_path / "memory.db"
    for key, value in {
        "APP_ENV": "development",
        "DATABASE_URL": f"sqlite+aiosqlite:///{path.as_posix()}",
        "SEED_DEMO_ON_STARTUP": "true",
        "RESET_DEMO_ON_STARTUP": "false",
        "LOCAL_EMBEDDING_ENABLED": "false",
        "CROSS_ENCODER_ENABLED": "false",
        "UNSTRUCTURED_ENABLED": "false",
        "MEMORY_RECENT_MESSAGES": "4",
    }.items():
        monkeypatch.setenv(key, value)

    def open_client():
        clear_settings_cache()
        AppStatus.should_exit_event = None
        return TestClient(create_app())

    yield open_client, path
    clear_settings_cache()


def ask(client, text, session_id=None, *, confirmation=None, mode="json", user="u_demo"):
    payload = {"message": text, "user_id": user}
    if session_id:
        payload["session_id"] = session_id
    if confirmation:
        payload["confirm_action_id"] = confirmation
    if mode == "sse":
        with client.stream("POST", "/api/v1/chat/stream", json=payload) as response:
            assert response.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        result = next(e["data"] for e in events if e["event"] == "answer")
        assert result["progress_nodes"][0] == "classify_intent"
        assert result["progress_nodes"][-1] == "compact_memory"
        assert "resolve_context" not in result["progress_nodes"]
        assert "apply_guardrail" not in result["progress_nodes"]
        return result
    response = client.post("/api/v1/chat", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["json", "sse"])
def test_slot_filling_and_confirmation_survive_restarts(memory_app, mode):
    open_client, path = memory_app
    with open_client() as client:
        first = ask(client, "我要把马克杯退掉，太丑了", mode=mode)
        session_id = first["session_id"]
        assert "订单号" in first["answer"]
        assert first["pending_action"] is None
    with open_client() as client:
        proposal = ask(client, "ORD10002", session_id, mode=mode)
        action = proposal["pending_action"]
        assert action["tool_name"] == "create_return_request"
        assert action["arguments"]["order_id"] == "ORD10002"
        assert "太丑了" in action["arguments"]["reason"]
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 0
    with open_client() as client:
        history = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").json()
        assert len(history["messages"][-1]["preparation_steps"]) == 6
        assert all(
            step["status"] == "completed" for step in history["messages"][-1]["preparation_steps"]
        )
        assert history["messages"][-1]["pending_action"]["action_id"] == action["action_id"]
        result = ask(client, "确认执行", session_id, confirmation=action["action_id"], mode=mode)
        assert result["pending_action"] is None
        ask(client, "确认执行", session_id, confirmation=action["action_id"], mode=mode)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT order_id FROM returns").fetchall() == [("ORD10002",)]
        history = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").json()
        assert all(message["pending_action"] is None for message in history["messages"])


def test_reference_resolution_owner_checks_and_new_chat_isolation(memory_app):
    open_client, path = memory_app
    with open_client() as client:
        first = ask(client, "查订单 ORD10003")
        session_id = first["session_id"]
        proposal = ask(client, "那就取消它", session_id)
        assert proposal["pending_action"]["arguments"]["order_id"] == "ORD10003"
        standalone = ask(client, "取消它")
        assert "订单号" in standalone["answer"]
        assert standalone["pending_action"] is None
        response = client.post(
            "/api/v1/chat", json={"message": "继续", "user_id": "other", "session_id": session_id}
        )
        assert response.status_code == 400
        for method, url in [
            ("get", f"/{session_id}"),
            ("delete", f"/{session_id}/memory"),
            ("delete", f"/{session_id}"),
        ]:
            assert (
                getattr(client, method)(f"/api/v1/sessions{url}?user_id=other").status_code == 400
            )
        assert client.get("/api/v1/sessions?user_id=other").json() == {"sessions": []}
        with sqlite3.connect(path) as db:
            assert (
                db.execute("SELECT status FROM orders WHERE order_id='ORD10003'").fetchone()[0]
                == "paid"
            )


def test_cancel_expire_and_supersede_pending_confirmations(memory_app):
    open_client, path = memory_app
    with open_client() as client:
        first = ask(client, "取消订单 ORD10003")
        session_id, action = first["session_id"], first["pending_action"]
        url = f"/api/v1/sessions/{session_id}/actions/{action['action_id']}?user_id=u_demo"
        assert client.delete(url).status_code == 204
        response = client.post(
            "/api/v1/chat",
            json={
                "message": "确认",
                "user_id": "u_demo",
                "session_id": session_id,
                "confirm_action_id": action["action_id"],
            },
        )
        assert response.status_code == 400
        second = ask(client, "取消订单 ORD10003", session_id)["pending_action"]
        third = ask(client, "取消订单 ORD10003", session_id)["pending_action"]
        assert (
            client.post(
                "/api/v1/chat",
                json={
                    "message": "确认",
                    "user_id": "u_demo",
                    "session_id": session_id,
                    "confirm_action_id": second["action_id"],
                },
            ).status_code
            == 400
        )
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE pending_actions SET expires_at='2000-01-01 00:00:00' WHERE action_id=?",
                (third["action_id"],),
            )
        assert (
            client.post(
                "/api/v1/chat",
                json={
                    "message": "确认",
                    "user_id": "u_demo",
                    "session_id": session_id,
                    "confirm_action_id": third["action_id"],
                },
            ).status_code
            == 400
        )


def test_forgetting_removes_context_without_erasing_visible_history(memory_app):
    open_client, path = memory_app
    with open_client() as client:
        session_id = ask(client, "查订单 ORD10003")["session_id"]
        for i in range(4):
            ask(client, f"你好{'！' * i}", session_id)
        remembered = ask(client, "还记得我的订单号是什么吗", session_id)
        assert "ORD10003" in remembered["answer"]
        before = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").json()
        assert (
            client.delete(f"/api/v1/sessions/{session_id}/memory?user_id=u_demo").status_code == 204
        )
        after = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").json()
        assert [m["content"] for m in before["messages"]] == [
            m["content"] for m in after["messages"]
        ]
    with open_client() as client:
        forgotten = ask(client, "取消它", session_id)
        assert "订单号" in forgotten["answer"]
        assert forgotten["pending_action"] is None
        with sqlite3.connect(path) as db:
            assert (
                "ORD10003"
                not in db.execute(
                    "SELECT summary FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0]
            )


def test_server_history_pagination_and_deleting_a_session(memory_app):
    open_client, _ = memory_app
    with open_client() as client:
        session_id = ask(client, "查订单 ORD10003")["session_id"]
        ask(client, "那就取消它", session_id)
        sessions = client.get("/api/v1/sessions?user_id=u_demo").json()["sessions"]
        assert any(s["session_id"] == session_id for s in sessions)
        latest = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo&limit=2").json()
        assert latest["has_more"]
        previous = client.get(
            f"/api/v1/sessions/{session_id}?user_id=u_demo&limit=2&before_id={latest['next_before_id']}"
        ).json()
        assert not previous["has_more"]
        assert len({m["id"] for m in latest["messages"] + previous["messages"]}) == 4
        assert client.delete(f"/api/v1/sessions/{session_id}?user_id=u_demo").status_code == 204
        assert client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").status_code == 400


def test_demo_reset_invalidates_old_confirmations(memory_app, monkeypatch):
    open_client, _ = memory_app
    with open_client() as client:
        proposal = ask(client, "取消订单 ORD10003")
    monkeypatch.setenv("RESET_DEMO_ON_STARTUP", "true")
    with open_client() as client:
        session_id = proposal["session_id"]
        assert (
            client.post(
                "/api/v1/chat",
                json={
                    "message": "确认",
                    "user_id": "u_demo",
                    "session_id": session_id,
                    "confirm_action_id": proposal["pending_action"]["action_id"],
                },
            ).status_code
            == 400
        )
        history = client.get(f"/api/v1/sessions/{session_id}?user_id=u_demo").json()
        assert all(m["pending_action"] is None for m in history["messages"])


def test_concurrent_confirmation_requests_do_not_duplicate_business_writes(memory_app):
    open_client, path = memory_app
    with open_client() as client:
        proposal = ask(client, "我要退货 ORD10002，尺码不合适")
        session_id = proposal["session_id"]
        action_id = proposal["pending_action"]["action_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(ask, client, "确认执行", session_id, confirmation=action_id)
                for _ in range(2)
            ]
            assert all(future.result()["pending_action"] is None for future in futures)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 1
