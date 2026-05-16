from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cc_tmux.server import CCTmuxService, create_app, openai_stream_events, session_event_stream


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

    def status(self, session_id, *, lines=80):
        self.calls.append(("status", {"session_id": session_id, "lines": lines}))
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

    def decisions(self, session_id):
        self.calls.append(("decisions", session_id))
        return []

    def post_decision(self, session_id, *, decision_id, option, feedback=None):
        self.calls.append(
            (
                "post_decision",
                {
                    "session_id": session_id,
                    "decision_id": decision_id,
                    "option": option,
                    "feedback": feedback,
                },
            )
        )
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "decision_id": decision_id,
            "option": option,
            "keys": [option, "Enter"],
        }

    def artifacts(self, session_id):
        self.calls.append(("artifacts", session_id))
        return {
            "session_id": session_id,
            "project_path": "/tmp/demo",
            "git_status_short": " M README.md",
            "changed_files": ["README.md"],
            "diff_stat": "README.md | 1 +",
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

    def structured_events(self, session_id, offset=0):
        self.calls.append(("structured_events", {"session_id": session_id, "offset": offset}))
        return {"session_id": session_id, "events": [], "offset": offset, "log_path": None}

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

    def chat_completion_stream_events(self, *, model, messages, metadata=None):
        self.calls.append(
            (
                "chat_completion_stream_events",
                {"model": model, "messages": messages, "metadata": metadata},
            )
        )
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
        }
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }


def make_client():
    service = FakeService()
    return TestClient(create_app(service)), service


def test_health():
    client, _service = make_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "auth_required": False}


def test_models_endpoint_openai_shape():
    client, _service = make_client()

    response = client.get("/v1/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["data"][0] == {
        "id": "claude-code",
        "object": "model",
        "owned_by": "cc-tmux",
    }


def test_v1_endpoints_are_unprotected_without_api_key():
    client = TestClient(create_app(FakeService()))

    assert client.get("/v1/sessions").status_code == 200


def test_v1_endpoints_require_bearer_when_api_key_configured():
    client = TestClient(create_app(FakeService(), api_key="secret"))

    assert client.get("/health").json() == {"status": "ok", "auth_required": True}
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/v1/models", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "claude-code"


def test_session_rest_flow_uses_service():
    client, service = make_client()

    assert client.post("/v1/sessions", json={"project_path": "/tmp/demo", "name": "demo"}).json()[
        "session_id"
    ] == "cc-tmux-demo"
    assert client.get("/v1/sessions").json()[0]["session"] == "cc-tmux-demo"
    status = client.get("/v1/sessions/cc-tmux-demo/status").json()
    assert status["last_prompt_ready"] is True
    assert status["state"] == "idle"
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


class FakeStopTmux:
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live or set()
        self.sent_keys: list[tuple[str, tuple[str, ...]]] = []
        self.killed: list[str] = []

    def list_sessions(self) -> list[str]:
        return sorted(self.live)

    def has_session(self, session_name: str) -> bool:
        return session_name in self.live

    def send_keys(self, target: str, *keys: str) -> None:
        self.sent_keys.append((target, keys))

    def kill_session(self, session_name: str) -> None:
        self.killed.append(session_name)
        self.live.discard(session_name)


def test_service_stop_session_is_idempotent_for_missing_session(monkeypatch):
    removed: list[str] = []
    monkeypatch.setattr("cc_tmux.server.remove_record", lambda session: removed.append(session))
    tmux = FakeStopTmux()
    service = CCTmuxService(tmux=tmux)  # type: ignore[arg-type]

    payload = service.stop_session("missing", wait_seconds=0)

    assert payload == {
        "session_id": "cc-tmux-missing",
        "name": "cc-tmux-missing",
        "session": "cc-tmux-missing",
        "stopped": True,
        "exists": False,
        "existed": False,
        "graceful": False,
    }
    assert tmux.sent_keys == []
    assert tmux.killed == []
    assert removed == ["cc-tmux-missing"]


def test_delete_missing_session_returns_200(monkeypatch):
    monkeypatch.setattr("cc_tmux.server.remove_record", lambda _session: None)
    service = CCTmuxService(tmux=FakeStopTmux())  # type: ignore[arg-type]
    client = TestClient(create_app(service))

    response = client.delete("/v1/sessions/already-stopped")

    assert response.status_code == 200
    assert response.json()["session_id"] == "cc-tmux-already-stopped"
    assert response.json()["stopped"] is True
    assert response.json()["exists"] is False


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


class FakeWaitService(CCTmuxService):
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = statuses
        self.seen: list[dict[str, Any]] = []

    def status(self, session_id: str, *, lines: int = 80) -> dict[str, Any]:
        assert session_id == "cc-tmux-demo"
        assert lines == 120
        payload = self.statuses.pop(0) if self.statuses else self.seen[-1]
        self.seen.append(payload)
        return payload


def test_wait_for_new_turn_ready_ignores_stale_ready(monkeypatch):
    service = FakeWaitService(
        [
            {"last_prompt_ready": True, "capture": "old ready\n│ ❯ "},
            {"last_prompt_ready": False, "capture": "running new turn"},
            {"last_prompt_ready": True, "capture": "assistant finished\n│ ❯ "},
        ]
    )
    monkeypatch.setattr("cc_tmux.server.time.sleep", lambda _seconds: None)

    assert service.wait_for_new_turn_ready(
        "cc-tmux-demo",
        timeout=5,
        baseline_capture="old ready\n│ ❯ ",
        interval=0,
        settle_seconds=0,
    ) is True

    assert service.seen == [
        {"last_prompt_ready": True, "capture": "old ready\n│ ❯ "},
        {"last_prompt_ready": False, "capture": "running new turn"},
        {"last_prompt_ready": True, "capture": "assistant finished\n│ ❯ "},
    ]


def test_wait_for_new_turn_ready_times_out_without_busy_lifecycle(monkeypatch):
    service = FakeWaitService(
        [
            {"last_prompt_ready": True, "capture": "old ready\n│ ❯ "},
            {"last_prompt_ready": True, "capture": "prompt echoed\n│ ❯ "},
        ]
    )
    monotonic_values = iter([0.0, 0.0, 0.1, 1.1])
    monkeypatch.setattr("cc_tmux.server.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("cc_tmux.server.time.sleep", lambda _seconds: None)

    assert service.wait_for_new_turn_ready(
        "cc-tmux-demo",
        timeout=1,
        baseline_capture="old ready\n│ ❯ ",
        interval=0,
        settle_seconds=0,
    ) is False

    assert len(service.seen) == 2


class FakeLogCompletionService(CCTmuxService):
    def __init__(self) -> None:
        self.tmux = type("T", (), {"send_text": lambda _self, _target, _content: None})()

    def _resolve_chat_target(self, messages, metadata):
        return "cc-tmux-demo", "do work", {}

    def send_message(self, session_id, *, content, wait_ready=True, timeout_seconds=120.0):
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "ready": True,
            "capture": "raw capture should not win",
        }

    def structured_events(self, session_id, offset=0):
        return {
            "session_id": session_id,
            "offset": offset,
            "log_path": "/tmp/fake.jsonl",
            "events": [{"type": "assistant_text", "text": "answer from logs"}],
        }


def test_service_chat_completion_uses_log_final_text():
    service = FakeLogCompletionService()

    payload = service.chat_completion(
        model="cc-tmux",
        messages=[{"role": "user", "content": "do work"}],
        metadata={"session": "cc-tmux-demo"},
    )

    assert payload["choices"][0]["message"]["content"] == "answer from logs"
    assert payload["metadata"]["log_path"] == "/tmp/fake.jsonl"


class FakeScopedLogCompletionService(CCTmuxService):
    def _resolve_chat_target(self, messages, metadata):
        return "cc-tmux-demo", "do work", {}

    def send_message(self, session_id, *, content, wait_ready=True, timeout_seconds=120.0):
        return {
            "session_id": session_id,
            "name": session_id,
            "session": session_id,
            "ready": True,
            "capture": "raw capture should not win",
        }

    def structured_events(self, session_id, offset=0):
        if offset == 0:
            return {
                "session_id": session_id,
                "offset": 100,
                "log_path": "/tmp/fake.jsonl",
                "events": [{"type": "assistant_text", "text": "old answer"}],
            }
        assert offset == 100
        return {
            "session_id": session_id,
            "offset": 200,
            "log_path": "/tmp/fake.jsonl",
            "events": [{"type": "assistant_text", "text": "current answer"}],
        }


def test_service_chat_completion_scopes_log_text_to_current_turn():
    service = FakeScopedLogCompletionService()

    payload = service.chat_completion(
        model="cc-tmux",
        messages=[{"role": "user", "content": "do work"}],
        metadata={"session": "cc-tmux-demo"},
    )

    assert payload["choices"][0]["message"]["content"] == "current answer"


def test_chat_completions_streaming_sse_shape():
    client, service = make_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "cc-tmux",
            "messages": [{"role": "user", "content": "do work"}],
            "metadata": {"session": "cc-tmux-demo"},
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert "data: {" in text
    assert '"object": "chat.completion.chunk"' in text
    assert '"content": "hello"' in text
    assert "data: [DONE]" in text
    assert service.calls[-1][0] == "chat_completion_stream_events"


def test_session_events_accepts_float_interval():
    app = create_app(FakeService())
    route = next(route for route in app.routes if route.path == "/v1/sessions/{session_id}/events")
    query_params = {field.name: field for field in route.dependant.query_params}

    assert "request" not in query_params
    interval, interval_errors = query_params["interval"].validate(
        "0.2", {}, loc=("query", "interval")
    )
    lines, lines_errors = query_params["n"].validate("40", {}, loc=("query", "n"))
    source, source_errors = query_params["source"].validate("logs", {}, loc=("query", "source"))

    assert not interval_errors
    assert interval == 0.2
    assert not lines_errors
    assert lines == 40
    assert not source_errors
    assert source == "logs"


def test_decisions_and_artifacts_routes_use_service():
    client, service = make_client()

    assert client.get("/v1/sessions/cc-tmux-demo/decisions").json() == []
    posted = client.post(
        "/v1/sessions/cc-tmux-demo/decisions",
        json={"decision_id": "plan_approval", "option": "2"},
    ).json()
    assert posted["keys"] == ["2", "Enter"]
    artifacts = client.get("/v1/sessions/cc-tmux-demo/artifacts").json()
    assert artifacts["changed_files"] == ["README.md"]
    assert ("artifacts", "cc-tmux-demo") in service.calls


def test_session_event_stream_emits_log_events_before_capture_fallback():
    statuses = iter(
        [
            {
                "session_id": "cc-tmux-demo",
                "exists": True,
                "last_prompt_ready": False,
                "plan_mode": False,
                "awaiting_plan_approval": False,
                "capture": "screen text",
            }
        ]
    )

    events = list(
        session_event_stream(
            lambda: next(statuses),
            structured_events_func=lambda offset: {
                "offset": offset + 10,
                "events": [{"type": "assistant_text", "text": "log text"}],
            },
            interval=0,
            max_ticks=1,
        )
    )

    assert 'event: assistant_text\n' in events[1]
    assert '"text": "log text"' in events[1]
    assert not any("capture_delta" in event for event in events)


def test_openai_stream_events_prefers_log_text_over_capture():
    statuses = iter(
        [
            {"exists": True, "last_prompt_ready": False, "capture": "old running"},
            {"exists": True, "last_prompt_ready": True, "capture": "old running done"},
        ]
    )
    calls = 0

    def structured(offset):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "offset": offset + 1,
                "events": [{"type": "assistant_text", "text": "from logs"}],
            }
        return {"offset": offset, "events": []}

    chunks = list(
        openai_stream_events(
            model="cc-tmux",
            session_id="cc-tmux-demo",
            status_func=lambda: next(statuses),
            structured_events_func=structured,
            baseline_capture="old",
            interval=0,
            timeout=5,
            sleep=lambda _seconds: None,
        )
    )

    assert chunks[0]["choices"][0]["delta"]["content"] == "from logs"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_session_event_stream_formats_status_delta_and_decision():
    statuses = iter(
        [
            {
                "session_id": "cc-tmux-demo",
                "exists": True,
                "last_prompt_ready": False,
                "plan_mode": False,
                "awaiting_plan_approval": False,
                "capture": "hello",
            },
            {
                "session_id": "cc-tmux-demo",
                "exists": True,
                "last_prompt_ready": False,
                "plan_mode": False,
                "awaiting_plan_approval": True,
                "capture": "hello world",
            },
        ]
    )

    events = list(session_event_stream(lambda: next(statuses), interval=0, max_ticks=2))

    assert events[0].startswith("event: status\ndata: ")
    assert '"state": "running"' in events[0]
    assert events[1] == 'event: capture_delta\ndata: {"text": "hello"}\n\n'
    assert 'event: decision_required\n' in events[-1]
    assert '"recommended_option": "2"' in events[-1]


def test_openai_stream_events_yields_deltas_stop_chunk():
    statuses = iter(
        [
            {"exists": True, "last_prompt_ready": True, "capture": "old"},
            {"exists": True, "last_prompt_ready": False, "capture": "old new"},
            {"exists": True, "last_prompt_ready": True, "capture": "old new done"},
        ]
    )

    chunks = list(
        openai_stream_events(
            model="cc-tmux",
            session_id="cc-tmux-demo",
            status_func=lambda: next(statuses),
            baseline_capture="old",
            interval=0,
            timeout=5,
            sleep=lambda _seconds: None,
        )
    )

    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"]["content"] == " new"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_service_decisions_use_plan_approval_status():
    service = FakeWaitService(
        [
            {
                "session_id": "cc-tmux-demo",
                "exists": True,
                "last_prompt_ready": False,
                "plan_mode": True,
                "awaiting_plan_approval": True,
                "plan_file": "/tmp/plan.md",
                "capture": "plan",
            }
        ]
    )

    decisions = service.decisions("cc-tmux-demo")

    assert decisions[0]["id"] == "plan_approval"
    assert decisions[0]["recommended_option"] == "2"
    assert decisions[0]["plan_file"] == "/tmp/plan.md"


def test_service_artifacts_git_and_non_git(tmp_path):
    service = CCTmuxService.__new__(CCTmuxService)

    non_git = tmp_path / "non-git"
    non_git.mkdir()
    non_git_payload = service.artifacts(project_path=str(non_git))
    assert non_git_payload["changed_files"] == []
    assert non_git_payload["error"] == "not a git repository"

    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=git_repo, check=True, capture_output=True)
    readme = git_repo / "README.md"
    readme.write_text("hello\n")

    payload = service.artifacts(project_path=str(git_repo))

    assert "error" not in payload
    assert "README.md" in payload["changed_files"]
    assert "README.md" in payload["git_status_short"]


def test_service_artifacts_resolves_short_session_and_untracked_files(tmp_path, monkeypatch):
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "REST_V02_OK.md").write_text("created live\n")

    from cc_tmux.state import SessionRecord

    monkeypatch.setattr(
        "cc_tmux.server.known_records",
        lambda: [SessionRecord.create(git_repo, "cc-tmux-serve-v02")],
    )

    service = CCTmuxService.__new__(CCTmuxService)
    payload = service.artifacts("serve-v02")

    assert payload["project_path"] == str(git_repo.resolve())
    assert payload["changed_files"] == ["REST_V02_OK.md"]
    assert "?? REST_V02_OK.md" in payload["git_status_short"]


class FakeArtifactsLogService(CCTmuxService):
    def __init__(self, touched_path: str) -> None:
        self.touched_path = touched_path

    def structured_events(self, session_id, offset=0):
        return {
            "session_id": session_id,
            "offset": offset,
            "log_path": "/tmp/fake.jsonl",
            "events": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": self.touched_path},
                }
            ],
        }


def test_service_artifacts_normalizes_absolute_tool_paths_and_dedupes(tmp_path):
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=git_repo, check=True, capture_output=True)
    written = git_repo / "LOG_NONSTREAM_OK.md"
    written.write_text("created live\n")

    service = FakeArtifactsLogService(str(written))
    payload = service.artifacts("cc-tmux-demo", project_path=str(git_repo))

    assert payload["changed_files"] == ["LOG_NONSTREAM_OK.md"]
    assert payload["tool_touched_files"] == ["LOG_NONSTREAM_OK.md"]


def test_service_artifacts_keeps_absolute_tool_paths_outside_project(tmp_path):
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    outside = tmp_path / "outside.txt"
    import subprocess

    subprocess.run(["git", "init"], cwd=git_repo, check=True, capture_output=True)

    service = FakeArtifactsLogService(str(outside))
    payload = service.artifacts("cc-tmux-demo", project_path=str(git_repo))

    assert payload["tool_touched_files"] == [str(outside)]
