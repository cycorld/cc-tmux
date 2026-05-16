from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .state import SessionRecord, remove_record, upsert_record
from .tmux import (
    CCTmuxError,
    Tmux,
    awaiting_plan_approval_heuristic,
    claude_command,
    known_records,
    normalize_session_name,
    plan_file_heuristic,
    plan_mode_heuristic,
    prompt_done_heuristic,
    prompt_ready_heuristic,
    resolve_session,
    slugify_project,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19410


class CCTmuxService:
    """Small synchronous service layer used by the HTTP API.

    The server intentionally reuses the same tmux/state primitives as the CLI and
    keeps web-framework details outside this class so tests can replace it with a
    fake without starting tmux or Claude Code.
    """

    def __init__(self, tmux: Tmux | None = None) -> None:
        self.tmux = tmux or Tmux()

    def start_session(
        self,
        *,
        project_path: str,
        name: str | None = None,
        prompt: str | None = None,
        permission_mode: str | None = "acceptEdits",
        auto_trust: bool = True,
        startup_timeout: float = 15.0,
        trust_delay: float = 1.0,
    ) -> dict[str, Any]:
        project = Path(project_path).expanduser().resolve()
        if not project.exists():
            raise CCTmuxError(f"project path does not exist: {project}")
        if not project.is_dir():
            raise CCTmuxError(f"project path is not a directory: {project}")

        self.tmux.require()
        session = normalize_session_name(name) if name else slugify_project(project)
        command = claude_command(permission_mode, [])

        if self.tmux.has_session(session):
            created = False
        else:
            if shutil.which("claude") is None:
                raise CCTmuxError("claude binary not found on PATH")
            self.tmux.new_session(session, project, command)
            created = True
            self._wait_for_pane_ready(session, timeout=startup_timeout)

        upsert_record(SessionRecord.create(project, session))

        if auto_trust:
            self.tmux.send_keys(f"{session}:0.0", "Enter")
            time.sleep(trust_delay)
        if prompt:
            self.tmux.send_text(f"{session}:0.0", prompt)

        return {
            "session_id": session,
            "name": session,
            "session": session,
            "project_path": str(project),
            "created": created,
            "exists": True,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        live = set(self.tmux.list_sessions())
        records = known_records()
        payload: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            if not record.session_name.startswith("cc-tmux-"):
                continue
            payload.append(
                {
                    "session_id": record.session_name,
                    "name": record.session_name,
                    "session": record.session_name,
                    "project_path": record.project_path,
                    "created_at": record.created_at,
                    "exists": record.session_name in live,
                }
            )
            seen.add(record.session_name)
        for session in sorted(
            name for name in live if name.startswith("cc-tmux-") and name not in seen
        ):
            payload.append(
                {
                    "session_id": session,
                    "name": session,
                    "session": session,
                    "project_path": None,
                    "created_at": None,
                    "exists": True,
                }
            )
        return payload

    def status(self, session_id: str, *, lines: int = 80) -> dict[str, Any]:
        try:
            session = resolve_session(session_id, self.tmux)
        except CCTmuxError:
            session = session_id if session_id.startswith("cc-tmux-") else f"cc-tmux-{session_id}"
            return {
                "identifier": session_id,
                "session_id": session,
                "name": session,
                "session": session,
                "exists": False,
                "done": False,
                "last_prompt_ready": False,
                "plan_mode": False,
                "awaiting_plan_approval": False,
                "plan_file": None,
                "capture": "",
            }
        exists = self.tmux.has_session(session)
        capture = self.tmux.capture(f"{session}:0.0", lines=lines) if exists else ""
        return {
            "identifier": session_id,
            "session_id": session,
            "name": session,
            "session": session,
            "exists": exists,
            "done": prompt_done_heuristic(capture),
            "last_prompt_ready": prompt_ready_heuristic(capture),
            "plan_mode": plan_mode_heuristic(capture),
            "awaiting_plan_approval": awaiting_plan_approval_heuristic(capture),
            "plan_file": plan_file_heuristic(capture),
            "capture": capture,
        }

    def capture(self, session_id: str, *, lines: int = 120, ansi: bool = False) -> dict[str, Any]:
        session = resolve_session(session_id, self.tmux)
        return {
            "session_id": session,
            "name": session,
            "session": session,
            "capture": self.tmux.capture(f"{session}:0.0", lines=lines, ansi=ansi),
        }

    def send_message(
        self,
        session_id: str,
        *,
        content: str,
        wait_ready: bool = True,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        session = resolve_session(session_id, self.tmux)
        before_status = self.status(session, lines=120) if wait_ready else None
        self.tmux.send_text(f"{session}:0.0", content)
        ready = None
        if wait_ready:
            ready = self.wait_for_new_turn_ready(
                session,
                timeout=timeout_seconds,
                baseline_capture=str((before_status or {}).get("capture") or ""),
            )
        status = self.status(session)
        capture = self.capture(session, lines=120, ansi=False)["capture"]
        return {
            "session_id": session,
            "name": session,
            "session": session,
            "ready": ready,
            "status": status,
            "capture": capture,
        }

    def interrupt(
        self,
        session_id: str,
        *,
        wait_ready: bool = False,
        timeout_seconds: float = 120.0,
        key: str = "Escape",
    ) -> dict[str, Any]:
        session = resolve_session(session_id, self.tmux)
        self.tmux.send_keys(f"{session}:0.0", key)
        ready = None
        if wait_ready:
            ready = self.wait_for_prompt_ready(session, timeout=timeout_seconds)
        status = self.status(session)
        return {
            "session_id": session,
            "name": session,
            "session": session,
            "ready": ready,
            "status": status,
        }

    def send_keys(self, session_id: str, *, keys: list[str]) -> dict[str, Any]:
        if not keys:
            raise CCTmuxError("keys must contain at least one tmux key name")
        session = resolve_session(session_id, self.tmux)
        self.tmux.send_keys(f"{session}:0.0", *keys)
        return {"session_id": session, "name": session, "session": session, "keys": keys}

    def stop_session(self, session_id: str, *, wait_seconds: float = 3.0) -> dict[str, Any]:
        session = resolve_session(session_id, self.tmux)
        target = f"{session}:0.0"
        graceful = False
        if self.tmux.has_session(session):
            self.tmux.send_keys(target, "/exit", "Enter")
            deadline = time.time() + max(wait_seconds, 0.0)
            while time.time() < deadline:
                if not self.tmux.has_session(session):
                    graceful = True
                    break
                time.sleep(0.25)
            if self.tmux.has_session(session):
                self.tmux.kill_session(session)
        remove_record(session)
        return {
            "session_id": session,
            "name": session,
            "session": session,
            "stopped": True,
            "graceful": graceful,
        }

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        content = _last_user_content(messages)
        if not content:
            raise CCTmuxError("messages must include a user message with content")

        session = metadata.get("session") or metadata.get("session_id")
        project_path = metadata.get("project_path")
        if project_path:
            started = self.start_session(
                project_path=str(project_path),
                name=str(session) if session else None,
                prompt=None,
                permission_mode=metadata.get("permission_mode", "acceptEdits"),
                auto_trust=bool(metadata.get("auto_trust", True)),
            )
            session = started["session_id"]
        if not session:
            raise CCTmuxError("metadata.project_path or metadata.session is required")

        result = self.send_message(
            str(session),
            content=content,
            wait_ready=bool(metadata.get("wait_ready", True)),
            timeout_seconds=float(metadata.get("timeout_seconds", 120.0)),
        )
        answer = _assistant_content_from_capture(str(result.get("capture") or ""))
        now = int(time.time())
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": now,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "metadata": {"session": result.get("session"), "ready": result.get("ready")},
        }

    def wait_for_new_turn_ready(
        self,
        session_id: str,
        *,
        timeout: float,
        baseline_capture: str = "",
        interval: float = 0.5,
        settle_seconds: float = 0.5,
    ) -> bool:
        """Wait until a newly submitted turn has cycled back to the prompt.

        ``last_prompt_ready`` is only a screen heuristic. Immediately after tmux sends a
        prompt, Claude Code can still be painting the previous idle prompt for a short
        moment. Returning on that stale state makes REST/OpenAI requests report success
        before the assistant has started, let alone completed. To avoid that false
        positive, require evidence of a new lifecycle: the capture must change from the
        pre-send baseline, the pane must look not-ready/busy at least once, and only
        then may a ready prompt complete the wait.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        if settle_seconds > 0:
            time.sleep(min(settle_seconds, max(0.0, deadline - time.monotonic())))

        capture_changed = False
        saw_not_ready = False
        while True:
            payload = self.status(session_id, lines=120)
            capture = str(payload.get("capture") or "")
            ready = bool(payload.get("last_prompt_ready"))
            if capture != baseline_capture:
                capture_changed = True
            if capture_changed and not ready:
                saw_not_ready = True
            if capture_changed and saw_not_ready and ready:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def wait_for_prompt_ready(
        self,
        session_id: str,
        *,
        timeout: float,
        interval: float = 0.5,
    ) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            payload = self.status(session_id)
            if bool(payload.get("last_prompt_ready")):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def _wait_for_pane_ready(self, session: str, *, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            capture = self.tmux.capture(f"{session}:0.0", lines=30)
            if capture.strip():
                return
            time.sleep(0.25)


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                return "\n".join(part for part in parts if part)
    return ""


def _assistant_content_from_capture(capture: str, *, max_chars: int = 6000) -> str:
    cleaned = capture.strip()
    if not cleaned:
        return "Transcript tail:"
    if len(cleaned) > max_chars:
        cleaned = cleaned[-max_chars:]
    return f"Transcript tail:\n{cleaned}"


def create_app(service: CCTmuxService | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - exercised by CLI when extras missing
        raise RuntimeError(
            "cc-tmux serve requires FastAPI and uvicorn. Install with: "
            "python -m pip install -e '.[server]'"
        ) from exc

    app = FastAPI(title="cc-tmux server", version="0.1.0")
    app.state.service = service or CCTmuxService()

    @app.exception_handler(CCTmuxError)
    async def handle_cc_tmux_error(_request, exc: CCTmuxError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions")
    def start_session(body: dict[str, Any]) -> dict[str, Any]:
        if "project_path" not in body:
            raise HTTPException(status_code=422, detail="project_path is required")
        return app.state.service.start_session(
            project_path=str(body["project_path"]),
            name=body.get("name"),
            prompt=body.get("prompt"),
            permission_mode=body.get("permission_mode", "acceptEdits"),
            auto_trust=bool(body.get("auto_trust", True)),
        )

    @app.get("/v1/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return app.state.service.list_sessions()

    @app.get("/v1/sessions/{session_id}/status")
    def session_status(session_id: str) -> dict[str, Any]:
        return app.state.service.status(session_id)

    @app.get("/v1/sessions/{session_id}/capture")
    def session_capture(
        session_id: str,
        n: int = Query(default=120, ge=1, le=5000),
        ansi: bool = False,
    ) -> dict[str, Any]:
        return app.state.service.capture(session_id, lines=n, ansi=ansi)

    @app.post("/v1/sessions/{session_id}/messages")
    def session_message(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        content = body.get("content")
        if not isinstance(content, str) or not content:
            raise HTTPException(status_code=422, detail="content is required")
        return app.state.service.send_message(
            session_id,
            content=content,
            wait_ready=bool(body.get("wait_ready", True)),
            timeout_seconds=float(body.get("timeout_seconds", 120.0)),
        )

    @app.post("/v1/sessions/{session_id}/interrupt")
    def session_interrupt(session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return app.state.service.interrupt(
            session_id,
            wait_ready=bool(body.get("wait_ready", False)),
            timeout_seconds=float(body.get("timeout_seconds", 120.0)),
        )

    @app.post("/v1/sessions/{session_id}/key")
    def session_key(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        keys = body.get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise HTTPException(status_code=422, detail="keys must be a list of strings")
        return app.state.service.send_keys(session_id, keys=keys)

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        return app.state.service.stop_session(session_id)

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("stream") is True:
            raise HTTPException(
                status_code=501,
                detail="stream=true is not supported by the cc-tmux MVP server",
            )
        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not model:
            raise HTTPException(status_code=422, detail="model is required")
        if not isinstance(messages, list):
            raise HTTPException(status_code=422, detail="messages must be a list")
        return app.state.service.chat_completion(
            model=model,
            messages=messages,
            metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        )

    return app


def run_server(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise RuntimeError(
            "cc-tmux serve requires uvicorn. Install with: "
            "python -m pip install -e '.[server]'"
        ) from exc

    app = create_app()
    uvicorn.run(app, host=host, port=port)
