import hmac
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from .claude_logs import (
    extract_final_assistant_text,
    latest_log_file,
    parse_log_objects,
    read_jsonl_events,
)
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
OPENAI_MODELS = [
    {"id": "claude-code", "object": "model", "owned_by": "cc-tmux"},
    {"id": "cc-tmux/claude-code", "object": "model", "owned_by": "cc-tmux"},
]
OPENAI_MODEL_ALIASES = {model["id"]: model for model in OPENAI_MODELS}
PLAN_APPROVAL_DECISION = {
    "id": "plan_approval",
    "kind": "plan_approval",
    "prompt": "Claude has written up a plan and is waiting for approval.",
    "options": [
        {"id": "1", "label": "Yes, auto-accept edits"},
        {"id": "2", "label": "Yes, manually approve edits"},
        {"id": "3", "label": "No, keep planning"},
        {"id": "4", "label": "Provide feedback"},
    ],
    "recommended_option": "2",
}


def derive_state(status: dict[str, Any]) -> str:
    if not bool(status.get("exists")):
        return "stopped"
    if bool(status.get("awaiting_plan_approval")):
        return "awaiting_plan_approval"
    if bool(status.get("plan_mode")):
        return "plan_mode"
    if bool(status.get("last_prompt_ready")):
        return "idle"
    return "running"


def with_state(status: dict[str, Any]) -> dict[str, Any]:
    payload = dict(status)
    payload["state"] = derive_state(payload)
    return payload


def sse_format(event: str, data: Any) -> str:
    encoded = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {encoded}\n\n"


def _capture_delta(previous: str, current: str) -> str:
    if not previous:
        return current
    if current.startswith(previous):
        return current[len(previous) :]
    return current if current != previous else ""


class _AssistantTurnTracker:
    """Track whether structured log events represent an unfinished tool/MCP turn."""

    def __init__(self) -> None:
        self.pending_tool_use_ids: set[str] = set()
        self.open_tool_uses = 0
        self.saw_tool_or_mcp_activity = False
        self.saw_assistant_text_after_activity = False
        self.last_usage_stop_reason: str | None = None

    def update(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "tool_use":
                self.saw_tool_or_mcp_activity = True
                self.saw_assistant_text_after_activity = False
                tool_use_id = event.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    self.pending_tool_use_ids.add(tool_use_id)
                else:
                    self.open_tool_uses += 1
            elif event_type == "tool_result":
                self.saw_tool_or_mcp_activity = True
                self.saw_assistant_text_after_activity = False
                tool_use_id = event.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    self.pending_tool_use_ids.discard(tool_use_id)
                elif self.open_tool_uses > 0:
                    self.open_tool_uses -= 1
            elif event_type == "usage":
                stop_reason = event.get("stop_reason")
                self.last_usage_stop_reason = str(stop_reason) if stop_reason else None
                if self.last_usage_stop_reason in {"tool_use", "pause_turn", "max_tokens"}:
                    self.saw_tool_or_mcp_activity = True
            elif _is_background_tool_event(event):
                self.saw_tool_or_mcp_activity = True
                self.saw_assistant_text_after_activity = False
            elif event_type == "assistant_text" and event.get("text"):
                if self.saw_tool_or_mcp_activity:
                    self.saw_assistant_text_after_activity = True

    @property
    def has_pending_tool_use(self) -> bool:
        return bool(self.pending_tool_use_ids) or self.open_tool_uses > 0

    @property
    def waiting_for_post_tool_assistant_text(self) -> bool:
        return self.saw_tool_or_mcp_activity and not self.saw_assistant_text_after_activity

    @property
    def in_progress(self) -> bool:
        return self.has_pending_tool_use or self.waiting_for_post_tool_assistant_text


def _is_background_tool_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "").lower()
    name = str(event.get("name") or "").lower()
    subtype = str(event.get("subtype") or "").lower()
    return any(
        marker in value
        for marker in ("mcp", "api_retry", "api_req", "tool")
        for value in (event_type, name, subtype)
    )


def _model_detail(model_id: str) -> dict[str, Any]:
    model = OPENAI_MODEL_ALIASES.get(model_id)
    if model is None:
        raise CCTmuxError(f"model not found: {model_id}")
    return {
        **model,
        "created": 0,
        "permission": [],
        "root": "claude-code",
        "parent": None,
    }


def _discovery_props() -> dict[str, Any]:
    return {
        "name": "cc-tmux",
        "version": "0.1.0",
        "models": [model["id"] for model in OPENAI_MODELS],
        "chat_completions": True,
        "streaming": True,
    }


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
        permission_mode: str | None = "auto",
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
            return with_state(
                {
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
            )
        exists = self.tmux.has_session(session)
        capture = self.tmux.capture(f"{session}:0.0", lines=lines) if exists else ""
        return with_state(
            {
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
        )

    def capture(self, session_id: str, *, lines: int = 120, ansi: bool = False) -> dict[str, Any]:
        session = resolve_session(session_id, self.tmux)
        return {
            "session_id": session,
            "name": session,
            "session": session,
            "capture": self.tmux.capture(f"{session}:0.0", lines=lines, ansi=ansi),
        }

    def _project_path_for_session(self, session_id: str) -> Path | None:
        session_candidates = {session_id}
        tmux = getattr(self, "tmux", None)
        if tmux is not None:
            with suppress(CCTmuxError):
                session_candidates.add(resolve_session(session_id, tmux))
        with suppress(CCTmuxError):
            session_candidates.add(normalize_session_name(session_id))
        for record in known_records():
            if record.session_name in session_candidates or session_id == record.project_path:
                return Path(record.project_path).expanduser().resolve()
        return None

    def log_events(self, session_id: str, offset: int = 0) -> dict[str, Any]:
        project = self._project_path_for_session(session_id)
        if project is None:
            return {"session_id": session_id, "events": [], "offset": offset, "log_path": None}
        log_path = latest_log_file(project)
        if log_path is None:
            return {"session_id": session_id, "events": [], "offset": offset, "log_path": None}
        events, new_offset = read_jsonl_events(log_path, offset=offset)
        return {
            "session_id": session_id,
            "events": events,
            "offset": new_offset,
            "log_path": str(log_path),
        }

    def structured_events(self, session_id: str, offset: int = 0) -> dict[str, Any]:
        payload = self.log_events(session_id, offset=offset)
        return {**payload, "events": parse_log_objects(payload["events"])}

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

    def decisions(self, session_id: str) -> list[dict[str, Any]]:
        status = self.status(session_id, lines=120)
        if not bool(status.get("awaiting_plan_approval")):
            return []
        decision = dict(PLAN_APPROVAL_DECISION)
        decision["session_id"] = status.get("session_id", session_id)
        structured = self.structured_events(session_id)
        plans = [event for event in structured["events"] if event.get("type") == "plan"]
        if plans:
            plan = plans[-1]
            if plan.get("plan_file"):
                decision["plan_file"] = plan["plan_file"]
            if plan.get("plan"):
                decision["plan"] = plan["plan"]
        if status.get("plan_file"):
            decision["plan_file"] = status["plan_file"]
        return [decision]

    def post_decision(
        self,
        session_id: str,
        *,
        decision_id: str,
        option: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        if decision_id != "plan_approval":
            raise CCTmuxError(f"unsupported decision_id: {decision_id}")
        if option not in {"1", "2", "3", "4"}:
            raise CCTmuxError("option must be one of: 1, 2, 3, 4")
        keys = [option, "Enter"]
        if option == "4" and feedback:
            keys.append(feedback)
            keys.append("Enter")
        sent = self.send_keys(session_id, keys=keys)
        return {
            "session_id": sent["session_id"],
            "name": sent["name"],
            "session": sent["session"],
            "decision_id": decision_id,
            "option": option,
            "feedback_sent": bool(option == "4" and feedback),
            "keys": keys,
        }

    def artifacts(
        self, session_id: str | None = None, *, project_path: str | None = None
    ) -> dict[str, Any]:
        if project_path is None and session_id is not None:
            session_candidates = {session_id}
            with suppress(CCTmuxError):
                session_candidates.add(normalize_session_name(session_id))
            for record in known_records():
                if record.session_name in session_candidates:
                    project_path = record.project_path
                    break
        payload: dict[str, Any] = {
            "session_id": session_id,
            "project_path": project_path,
            "git_status_short": "",
            "changed_files": [],
            "diff_stat": "",
        }
        if not project_path:
            payload["error"] = "project_path unknown"
            return payload
        project = Path(project_path).expanduser().resolve()
        payload["project_path"] = str(project)
        if not project.exists() or not project.is_dir():
            payload["error"] = "project_path is not a directory"
            return payload
        if not (project / ".git").exists():
            payload["error"] = "not a git repository"
            return payload
        try:
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=project,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.rstrip()
            diff_stat = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=project,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.rstrip()
        except (OSError, subprocess.SubprocessError) as exc:
            payload["error"] = str(exc)
            return payload
        payload["git_status_short"] = status
        payload["changed_files"] = [
            _status_path(line) for line in status.splitlines() if line.strip()
        ]
        if session_id is not None:
            structured = self.structured_events(session_id)
            touched = _tool_touched_files(structured["events"], project=project)
            payload["tool_touched_files"] = touched
            payload["changed_files"] = sorted(set(payload["changed_files"]) | set(touched))
        payload["diff_stat"] = diff_stat
        return payload

    def stop_session(self, session_id: str, *, wait_seconds: float = 3.0) -> dict[str, Any]:
        try:
            session = resolve_session(session_id, self.tmux)
        except CCTmuxError:
            session = normalize_session_name(session_id)
        target = f"{session}:0.0"
        graceful = False
        existed = self.tmux.has_session(session)
        if existed:
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
            "exists": False,
            "existed": existed,
            "graceful": graceful,
        }

    def _resolve_chat_target(
        self, messages: list[dict[str, Any]], metadata: dict[str, Any] | None
    ) -> tuple[str, str, dict[str, Any]]:
        metadata = metadata or {}
        content = _last_user_content(messages)
        if not content:
            raise CCTmuxError("messages must include a user message with content")

        session = metadata.get("session") or metadata.get("session_id")
        project_path = metadata.get("project_path")
        started: dict[str, Any] = {}
        if project_path:
            started = self.start_session(
                project_path=str(project_path),
                name=str(session) if session else None,
                prompt=None,
                permission_mode=metadata.get("permission_mode", "auto"),
                auto_trust=bool(metadata.get("auto_trust", True)),
            )
            session = started["session_id"]
        if not session:
            raise CCTmuxError("metadata.project_path or metadata.session is required")
        return str(session), content, started

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        session, content, _started = self._resolve_chat_target(messages, metadata)
        baseline_structured = self.structured_events(session)
        log_offset = int(baseline_structured.get("offset") or 0)
        log_path = baseline_structured.get("log_path")

        result = self.send_message(
            session,
            content=content,
            wait_ready=bool(metadata.get("wait_ready", True)),
            timeout_seconds=float(metadata.get("timeout_seconds", 120.0)),
        )

        # If Claude Code got stuck in Plan Approval mode, auto-accept and retry
        if not result.get("ready") and self._is_awaiting_plan_approval(session):
            self.post_decision(session, decision_id="plan_approval", option="1")
            time.sleep(1)
            result = self.send_message(
                session,
                content="",
                wait_ready=True,
                timeout_seconds=float(metadata.get("timeout_seconds", 120.0)),
            )

        structured = self._wait_for_assistant_log_text(
            session,
            offset=log_offset,
            baseline_log_path=str(log_path) if log_path else None,
            timeout=float(metadata.get("log_settle_timeout", 3.0)),
        )
        answer = extract_final_assistant_text(structured["events"])
        if not answer:
            answer = _empty_assistant_log_message(structured.get("log_path"))
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
            "metadata": {
                "session": result.get("session"),
                "ready": result.get("ready"),
                "log_path": structured.get("log_path"),
            },
        }

    def _is_awaiting_plan_approval(self, session_id: str) -> bool:
        status = self.status(session_id, lines=120)
        return bool(status.get("awaiting_plan_approval"))

    def _wait_for_assistant_log_text(
        self,
        session_id: str,
        *,
        offset: int,
        baseline_log_path: str | None = None,
        timeout: float = 3.0,
        interval: float = 0.2,
    ) -> dict[str, Any]:
        """Read current-turn structured events, briefly waiting for log flush.

        OpenAI-compatible responses must be generated from Claude Code's JSONL
        assistant text events. The tmux pane is only a control/status surface and
        can contain TUI chrome (banner, spinner/status words, prompt glyphs, box
        drawing, status bar). Never fall back to pane capture for answer content.
        """

        deadline = time.monotonic() + max(timeout, 0.0)
        tracker = _AssistantTurnTracker()
        collected_events: list[dict[str, Any]] = []
        latest: dict[str, Any] = {
            "session_id": session_id,
            "events": [],
            "offset": offset,
            "log_path": None,
        }
        while True:
            latest = self.structured_events(session_id, offset=offset)
            latest_log_path = latest.get("log_path")
            if (
                baseline_log_path
                and latest_log_path
                and str(latest_log_path) != baseline_log_path
                and offset != 0
            ):
                latest = self.structured_events(session_id, offset=0)
                offset = 0
                baseline_log_path = str(latest_log_path)
                collected_events = []
                tracker = _AssistantTurnTracker()
            events = list(latest.get("events", []))
            if events:
                collected_events.extend(events)
                tracker.update(events)
            latest = {**latest, "events": collected_events}
            if extract_final_assistant_text(collected_events) and not tracker.in_progress:
                return latest
            if time.monotonic() >= deadline:
                return latest
            time.sleep(min(max(interval, 0.0), max(0.0, deadline - time.monotonic())))

    def chat_completion_stream_events(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        metadata = metadata or {}
        session, content, _started = self._resolve_chat_target(messages, metadata)
        structured = self.structured_events(session)
        log_offset = int(structured.get("offset") or 0)
        log_path = structured.get("log_path")
        self.tmux.send_text(f"{session}:0.0", content)
        yield from openai_stream_events(
            model=model,
            session_id=session,
            status_func=lambda: self.status(session, lines=120),
            structured_events_func=lambda offset: self.structured_events(session, offset=offset),
            baseline_log_offset=log_offset,
            baseline_log_path=str(log_path) if log_path else None,
            interval=float(metadata.get("stream_interval", 0.5)),
            timeout=float(metadata.get("timeout_seconds", 120.0)),
            stream_settle_seconds=float(
                metadata.get("stream_settle_seconds", metadata.get("log_settle_seconds", 0.5))
            ),
        )

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


def session_event_stream(
    status_func: Callable[[], dict[str, Any]],
    *,
    structured_events_func: Callable[[int], dict[str, Any]] | None = None,
    source: str = "auto",
    initial_log_offset: int = 0,
    interval: float = 1.0,
    max_ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[str]:
    previous_capture = ""
    log_offset = initial_log_offset
    tick = 0
    while max_ticks is None or tick < max_ticks:
        status = with_state(status_func())
        capture = str(status.get("capture") or "")
        status_subset = {
            key: status.get(key)
            for key in (
                "session_id",
                "name",
                "session",
                "exists",
                "done",
                "last_prompt_ready",
                "plan_mode",
                "awaiting_plan_approval",
                "plan_file",
                "state",
            )
            if key in status
        }
        yield sse_format("status", status_subset)
        log_event_count = 0
        if structured_events_func is not None and source in {"auto", "logs"}:
            structured = structured_events_func(log_offset)
            log_offset = int(structured.get("offset") or log_offset)
            for event in structured.get("events", []):
                log_event_count += 1
                yield sse_format(str(event.get("type") or "log_event"), event)
        if source != "logs":
            delta = _capture_delta(previous_capture, capture)
            if delta and (source == "capture" or log_event_count == 0):
                yield sse_format("capture_delta", {"text": delta})
        if bool(status.get("awaiting_plan_approval")):
            decision = dict(PLAN_APPROVAL_DECISION)
            decision["session_id"] = status.get("session_id")
            if status.get("plan_file"):
                decision["plan_file"] = status["plan_file"]
            yield sse_format("decision_required", decision)
        previous_capture = capture
        tick += 1
        if max_ticks is None or tick < max_ticks:
            sleep(max(interval, 0.0))


def openai_stream_events(
    *,
    model: str,
    session_id: str,
    status_func: Callable[[], dict[str, Any]],
    structured_events_func: Callable[[int], dict[str, Any]] | None = None,
    baseline_log_offset: int = 0,
    baseline_log_path: str | None = None,
    interval: float = 0.5,
    timeout: float = 120.0,
    stream_settle_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[dict[str, Any]]:
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    log_offset = baseline_log_offset
    saw_not_ready = False
    saw_ready_after_work = False
    emitted_text = False
    turn_tracker = _AssistantTurnTracker()
    deadline = time.monotonic() + max(timeout, 0.0)
    assistant_settle_deadline: float | None = None
    emitted_text_settle_deadline: float | None = None
    stream_settle_seconds = max(stream_settle_seconds, 0.0)
    while True:
        status = with_state(status_func())
        ready = bool(status.get("last_prompt_ready"))
        if not ready:
            saw_not_ready = True
        if saw_not_ready and ready:
            saw_ready_after_work = True
        if structured_events_func is not None:
            structured = structured_events_func(log_offset)
            current_log_path = structured.get("log_path")
            if (
                baseline_log_path
                and current_log_path
                and str(current_log_path) != baseline_log_path
                and log_offset != 0
            ):
                structured = structured_events_func(0)
                log_offset = 0
                baseline_log_path = str(current_log_path)
                turn_tracker = _AssistantTurnTracker()
            log_offset = int(structured.get("offset") or log_offset)
            events = structured.get("events", [])
            turn_tracker.update(events)
            if emitted_text and events:
                emitted_text_settle_deadline = None
            for event in events:
                if event.get("type") == "assistant_text" and event.get("text"):
                    emitted_text = True
                    emitted_text_settle_deadline = None
                    yield _openai_stream_chunk(
                        chunk_id=chunk_id,
                        created=created,
                        model=model,
                        content=str(event["text"]),
                        finish_reason=None,
                    )
        now = time.monotonic()
        if emitted_text:
            if saw_ready_after_work and not turn_tracker.in_progress:
                break
            if not turn_tracker.in_progress:
                if emitted_text_settle_deadline is None:
                    emitted_text_settle_deadline = now + min(
                        stream_settle_seconds, max(0.0, deadline - now)
                    )
                if emitted_text_settle_deadline is not None and now >= emitted_text_settle_deadline:
                    break
        elif saw_ready_after_work and not turn_tracker.in_progress:
            if assistant_settle_deadline is None:
                assistant_settle_deadline = now + min(3.0, max(0.0, deadline - now))
            elif now >= assistant_settle_deadline:
                break
        if now >= deadline:
            break
        sleep_until = deadline
        if assistant_settle_deadline is not None:
            sleep_until = min(sleep_until, assistant_settle_deadline)
        if emitted_text_settle_deadline is not None:
            sleep_until = min(sleep_until, emitted_text_settle_deadline)
        sleep(min(max(interval, 0.0), max(0.0, sleep_until - time.monotonic())))
    if not emitted_text:
        yield _openai_stream_chunk(
            chunk_id=chunk_id,
            created=created,
            model=model,
            content=_empty_assistant_log_message(None),
            finish_reason=None,
        )
    yield _openai_stream_chunk(
        chunk_id=chunk_id,
        created=created,
        model=model,
        content="",
        finish_reason="stop",
    )


def _openai_stream_chunk(
    *, chunk_id: str, created: int, model: str, content: str, finish_reason: str | None
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": {}, "finish_reason": finish_reason}
    if content:
        choice["delta"] = {"content": content}
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }


def _status_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line.strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()


def _tool_touched_files(events: list[dict[str, Any]], *, project: Path | None = None) -> list[str]:
    touched: set[str] = set()
    for event in events:
        if event.get("type") != "tool_use" or event.get("name") not in {
            "Write",
            "Edit",
            "MultiEdit",
        }:
            continue
        input_value = event.get("input")
        if not isinstance(input_value, dict):
            continue
        path = input_value.get("file_path") or input_value.get("path")
        if isinstance(path, str) and path:
            touched.add(_normalize_touched_path(path, project=project))
    return sorted(touched)


def _normalize_touched_path(path: str, *, project: Path | None = None) -> str:
    candidate = Path(path).expanduser()
    if project is not None:
        project = project.expanduser().resolve()
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            try:
                return resolved.relative_to(project).as_posix()
            except ValueError:
                return str(resolved)
    return candidate.as_posix()


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


def _empty_assistant_log_message(log_path: Any) -> str:
    location = f" at {log_path}" if log_path else ""
    return f"No assistant text was found in structured JSONL logs{location}."


def _authorized_bearer(authorization: str | None, api_key: str) -> bool:
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token, api_key)


def create_app(service: CCTmuxService | None = None, *, api_key: str | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Query, Request
        from fastapi.responses import JSONResponse, StreamingResponse
    except ImportError as exc:  # pragma: no cover - exercised by CLI when extras missing
        raise RuntimeError(
            "cc-tmux serve requires FastAPI and uvicorn. Install with: "
            "python -m pip install -e '.[server]'"
        ) from exc

    app = FastAPI(title="cc-tmux server", version="0.1.0")
    app.state.service = service or CCTmuxService()
    app.state.auth_required = bool(api_key)

    @app.middleware("http")
    async def require_bearer_auth(request: Request, call_next):
        if api_key and request.url.path.startswith("/v1/") and not _authorized_bearer(
            request.headers.get("authorization"), api_key
        ):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "missing or invalid bearer token"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.exception_handler(CCTmuxError)
    async def handle_cc_tmux_error(_request, exc: CCTmuxError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "auth_required": bool(api_key)}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": OPENAI_MODELS}

    @app.get("/v1/models/{model_id:path}")
    def model_detail(model_id: str) -> dict[str, Any]:
        return _model_detail(model_id)

    @app.get("/api/v1/models")
    def api_v1_models() -> dict[str, Any]:
        return {"object": "list", "data": OPENAI_MODELS}

    @app.get("/api/tags")
    def api_tags() -> dict[str, Any]:
        return {"models": [{"name": model["id"], **model} for model in OPENAI_MODELS]}

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"version": "0.1.0"}

    @app.get("/props")
    @app.get("/v1/props")
    def props() -> dict[str, Any]:
        return _discovery_props()

    @app.post("/v1/sessions")
    def start_session(body: dict[str, Any]) -> dict[str, Any]:
        if "project_path" not in body:
            raise HTTPException(status_code=422, detail="project_path is required")
        return app.state.service.start_session(
            project_path=str(body["project_path"]),
            name=body.get("name"),
            prompt=body.get("prompt"),
            permission_mode=body.get("permission_mode", "auto"),
            auto_trust=bool(body.get("auto_trust", True)),
        )

    @app.get("/v1/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return app.state.service.list_sessions()

    @app.get("/v1/sessions/{session_id}/status")
    def session_status(session_id: str) -> dict[str, Any]:
        return with_state(app.state.service.status(session_id))

    @app.get("/v1/sessions/{session_id}/events")
    async def session_events(
        request: Request,
        session_id: str,
        interval: Annotated[float, Query(ge=0.1, le=30.0)] = 1.0,
        n: Annotated[int, Query(ge=1, le=5000)] = 120,
        source: Annotated[str, Query(pattern="^(auto|logs|capture)$")] = "auto",
    ):
        async def event_generator():
            log_offset = 0
            while not await request.is_disconnected():
                def structured(offset: int):
                    nonlocal log_offset
                    payload = app.state.service.structured_events(session_id, offset=offset)
                    log_offset = int(payload.get("offset") or log_offset)
                    return payload

                for event in session_event_stream(
                    lambda: app.state.service.status(session_id, lines=n),
                    structured_events_func=(
                        structured if hasattr(app.state.service, "structured_events") else None
                    ),
                    source=source,
                    initial_log_offset=log_offset,
                    interval=interval,
                    max_ticks=1,
                ):
                    yield event
                import asyncio

                await asyncio.sleep(interval)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/v1/sessions/{session_id}/capture")
    def session_capture(
        session_id: str,
        n: Annotated[int, Query(ge=1, le=5000)] = 120,
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

    @app.get("/v1/sessions/{session_id}/decisions")
    def session_decisions(session_id: str) -> list[dict[str, Any]]:
        return app.state.service.decisions(session_id)

    @app.post("/v1/sessions/{session_id}/decisions")
    def session_post_decision(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        decision_id = body.get("decision_id")
        option = body.get("option")
        if not isinstance(decision_id, str) or not decision_id:
            raise HTTPException(status_code=422, detail="decision_id is required")
        if not isinstance(option, str) or not option:
            raise HTTPException(status_code=422, detail="option is required")
        feedback = body.get("feedback")
        if feedback is not None and not isinstance(feedback, str):
            raise HTTPException(status_code=422, detail="feedback must be a string")
        return app.state.service.post_decision(
            session_id, decision_id=decision_id, option=option, feedback=feedback
        )

    @app.get("/v1/sessions/{session_id}/artifacts")
    def session_artifacts(session_id: str) -> dict[str, Any]:
        return app.state.service.artifacts(session_id)

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        return app.state.service.stop_session(session_id)

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict[str, Any]):
        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not model:
            raise HTTPException(status_code=422, detail="model is required")
        if not isinstance(messages, list):
            raise HTTPException(status_code=422, detail="messages must be a list")

        # Support both direct "metadata" and OpenAI "extra_body.metadata"
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        extra = body.get("extra_body")
        if isinstance(extra, dict):
            extra_meta = extra.get("metadata")
            if isinstance(extra_meta, dict):
                meta = {**meta, **extra_meta}

        # Provide sane defaults so non-cc-tmux clients (Hermes, OpenAI SDK, etc.) work
        if not meta.get("project_path"):
            meta["project_path"] = "/tmp"
        if not meta.get("session"):
            meta["session"] = f"hermes-{uuid.uuid4().hex[:12]}"

        if body.get("stream") is True:
            def stream_generator():
                for chunk in app.state.service.chat_completion_stream_events(
                    model=model, messages=messages, metadata=meta
                ):
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream_generator(), media_type="text/event-stream")
        return app.state.service.chat_completion(
            model=model,
            messages=messages,
            metadata=meta,
        )

    return app


def run_server(
    *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, api_key: str | None = None
) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        raise RuntimeError(
            "cc-tmux serve requires uvicorn. Install with: "
            "python -m pip install -e '.[server]'"
        ) from exc

    app = create_app(api_key=api_key)
    uvicorn.run(app, host=host, port=port)
