from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cc_tmux.server import create_app


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.sessions = [
            {
                "session_id": "cc-tmux-demo",
                "name": "cc-tmux-demo",
                "session": "cc-tmux-demo",
                "project_path": "/tmp/demo",
                "exists": True,
            }
        ]

    def start_session(self, **kwargs):
        self.calls.append(("start_session", kwargs))
        return {
            "session_id": "cc-tmux-demo",
            "name": "cc-tmux-demo",
            "session": "cc-tmux-demo",
            "created": True,
            "exists": True,
        }

    def list_sessions(self):
        self.calls.append(("list_sessions", None))
        return self.sessions

    def status(self, session_id):
        self.calls.append(("status", session_id))
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "exists": True,
            "last_prompt_ready": True,
            "plan_mode": False,
            "awaiting_plan_approval": False,
            "plan_file": None,
            "capture": "ready\n│ ❯ ",
        }

    def capture(self, session_id, *, lines=120, ansi=False):
        self.calls.append(("capture", {"session_id": session_id, "lines": lines, "ansi": ansi}))
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "capture": "tail",
        }

    def send_message(self, session_id, *, content, wait_ready=True, timeout_seconds=120.0):
        self.calls.append(
            (
                "send_message",
                {
                    "session_id": session_id,
                    "content": content,
                    "wait_ready": wait_ready,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "ready": wait_ready,
            "status": {"last_prompt_ready": wait_ready},
            "capture": "assistant finished\n│ ❯ ",
        }

    def interrupt(self, session_id, *, wait_ready=False, timeout_seconds=120.0):
        self.calls.append(
            (
                "interrupt",
                {
                    "session_id": session_id,
                    "wait_ready": wait_ready,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "ready": wait_ready,
        }

    def send_keys(self, session_id, *, keys):
        self.calls.append(("send_keys", {"session_id": session_id, "keys": keys}))
        return {"session_id": session_id, "name": session_id, "session": session_id, "keys": keys}

    def stop_session(self, session_id):
        self.calls.append(("stop_session", session_id))
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "stopped": True,
        }

    def chat_completion(self, *, model, messages, metadata=None):
        self.calls.append(
            ("chat_completion", {"model": model, "messages": messages, "metadata": metadata})
        )
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Transcript tail:\nassistant finished",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def make_client():
    service = FakeService()
    return TestClient(create_app(service)), service


def test_health():
    client, _service = make_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_rest_flow_uses_service():
    client, service = make_client()

    assert client.post("/v1/sessions", json={"project_path": "/tmp/demo", "name": "demo"}).json()[
        "session_id"
    ] == "cc-tmux-demo"
    assert client.get("/v1/sessions").json()[0]["session"] == "cc-tmux-demo"
    assert client.get("/v1/sessions/cc-tmux-demo/status").json()["last_prompt_ready"] is True
    assert client.get("/v1/sessions/cc-tmux-demo/capture?n=5&ansi=true").json()["capture"] == "tail"
    assert client.post(
        "/v1/sessions/cc-tmux-demo/messages",
        json={"content": "hello", "wait_ready": False, "timeout_seconds": 2},
    ).json()["status"] == {"last_prompt_ready": False}
    assert client.post(
        "/v1/sessions/cc-tmux-demo/interrupt",
        json={"wait_ready": True, "timeout_seconds": 3},
    ).json()["ready"] is True
    assert client.post("/v1/sessions/cc-tmux-demo/key", json={"keys": ["Escape"]}).json()[
        "keys"
    ] == ["Escape"]
    assert client.delete("/v1/sessions/cc-tmux-demo").json()["stopped"] is True

    assert ("capture", {"session_id": "cc-tmux-demo", "lines": 5, "ansi": True}) in service.calls
    assert (
        "send_message",
        {
            "session_id": "cc-tmux-demo",
            "content": "hello",
            "wait_ready": False,
            "timeout_seconds": 2.0,
        },
    ) in service.calls


def test_chat_completions_non_stream_shape():
    client, service = make_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "cc-tmux",
            "messages": [{"role": "user", "content": "do work"}],
            "metadata": {"session": "cc-tmux-demo"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["role"] == "assistant"
    assert payload["choices"][0]["message"]["content"].startswith("Transcript tail:")
    assert payload["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert service.calls[-1][0] == "chat_completion"


def test_chat_completions_rejects_streaming():
    client, _service = make_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "cc-tmux", "messages": [], "stream": True},
    )

    assert response.status_code == 501
    assert "stream=true is not supported" in response.json()["detail"]
