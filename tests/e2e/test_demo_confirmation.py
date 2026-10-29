import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app


@pytest.mark.parametrize("reset_enabled", [True, False])
def test_development_startup_and_order_confirmation(monkeypatch, tmp_path, reset_enabled):
    database_path = tmp_path / "demo.db"
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SEED_DEMO_ON_STARTUP", "true")
    monkeypatch.setenv("RESET_DEMO_ON_STARTUP", str(reset_enabled).lower())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("LOCAL_EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("CROSS_ENCODER_ENABLED", "false")
    monkeypatch.setenv("UNSTRUCTURED_ENABLED", "false")
    clear_settings_cache()

    def order_status():
        with sqlite3.connect(database_path) as db:
            return db.execute(
                "SELECT status FROM orders WHERE order_id = ?", ("ORD10003",)
            ).fetchone()[0]

    try:
        with TestClient(create_app()) as client:
            assert order_status() == "paid"
            response = client.post(
                "/api/v1/chat",
                json={"message": "取消订单 ORD10003", "user_id": "u_demo"},
            )
            assert response.status_code == 200
            proposal = response.json()
            assert proposal["pending_action"]["tool_name"] == "cancel_order"
            assert order_status() == "paid"

            response = client.post(
                "/api/v1/chat",
                json={
                    "message": "确认执行",
                    "user_id": "u_demo",
                    "session_id": proposal["session_id"],
                    "confirm_action_id": proposal["pending_action"]["action_id"],
                },
            )
            assert response.status_code == 200
            assert response.json()["pending_action"] is None
            assert order_status() == "cancelled"

        # Restart restores demo state by default; disabling reset preserves changes.
        clear_settings_cache()
        with TestClient(create_app()) as client:
            assert order_status() == ("paid" if reset_enabled else "cancelled")
            with sqlite3.connect(database_path) as db:
                assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3
            if reset_enabled:
                response = client.post(
                    "/api/v1/chat",
                    json={"message": "取消订单 ORD10003", "user_id": "u_demo"},
                )
                assert response.status_code == 200
                proposal = response.json()
                response = client.post(
                    "/api/v1/chat",
                    json={
                        "message": "确认执行",
                        "user_id": "u_demo",
                        "session_id": proposal["session_id"],
                        "confirm_action_id": proposal["pending_action"]["action_id"],
                    },
                )
                assert response.status_code == 200
                assert order_status() == "cancelled"
    finally:
        clear_settings_cache()


def test_colloquial_return_requires_order_and_confirmation_over_sse(monkeypatch, tmp_path):
    database_path = tmp_path / "return.db"
    for key, value in {
        "APP_ENV": "development",
        "SEED_DEMO_ON_STARTUP": "true",
        "RESET_DEMO_ON_STARTUP": "true",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "LOCAL_EMBEDDING_ENABLED": "false",
        "CROSS_ENCODER_ENABLED": "false",
        "UNSTRUCTURED_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()

    def stream(client, payload):
        with client.stream("POST", "/api/v1/chat/stream", json=payload) as response:
            assert response.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in response.iter_lines()
                if line.startswith("data:")
            ]
        nodes = [event["node"] for event in events if event["event"] == "node"]
        assert "plan_tasks" in nodes
        assert "execute_tools" in nodes
        assert next(i for i, event in enumerate(events) if event["event"] == "node") < next(
            i for i, event in enumerate(events) if event["event"] == "answer"
        )
        return next(event["data"] for event in events if event["event"] == "answer")

    try:
        with TestClient(create_app()) as client:
            missing = stream(client, {"message": "我要把马克杯退掉，太丑了", "user_id": "u_demo"})
            assert missing["pending_action"] is None
            assert "订单号" in missing["answer"]
            proposal = stream(
                client,
                {
                    "message": "我要把订单 ORD10002 的马克杯退掉，太丑了",
                    "user_id": "u_demo",
                    "session_id": missing["session_id"],
                },
            )
            assert proposal["pending_action"]["tool_name"] == "create_return_request"
            with sqlite3.connect(database_path) as db:
                assert db.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 0
            confirmed = stream(
                client,
                {
                    "message": "确认执行",
                    "user_id": "u_demo",
                    "session_id": proposal["session_id"],
                    "confirm_action_id": proposal["pending_action"]["action_id"],
                },
            )
            assert confirmed["pending_action"] is None
            with sqlite3.connect(database_path) as db:
                assert db.execute("SELECT order_id FROM returns").fetchall() == [("ORD10002",)]
    finally:
        clear_settings_cache()
