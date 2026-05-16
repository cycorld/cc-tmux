from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LOG_ROOT = Path.home() / ".claude" / "projects"


def project_log_dir(project_path: Path) -> Path:
    """Return Claude Code's best-effort JSONL log directory for a project path.

    Claude Code currently maps absolute project paths under ``~/.claude/projects`` by
    replacing path separators with ``-`` (for example ``/tmp/foo`` becomes
    ``-tmp-foo``). This is an internal, undocumented format, so callers should treat
    absence or parse failures as non-fatal and fall back to tmux capture.
    """

    encoded = str(project_path.expanduser().resolve()).replace("/", "-")
    return LOG_ROOT / encoded


def list_log_files(project_path: Path) -> list[Path]:
    log_dir = project_log_dir(project_path)
    if not log_dir.exists() or not log_dir.is_dir():
        return []
    return sorted(
        (path for path in log_dir.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def latest_log_file(project_path: Path) -> Path | None:
    files = list_log_files(project_path)
    return files[0] if files else None


def read_jsonl_events(log_path: Path, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read JSON objects from a JSONL log using a byte offset.

    Invalid or partially-written trailing lines are ignored; the returned offset only
    advances past complete lines that were read from disk.
    """

    events: list[dict[str, Any]] = []
    if offset < 0:
        offset = 0
    try:
        with log_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line:
                    return events, handle.tell()
                next_offset = handle.tell()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Do not advance past an unterminated trailing write.
                    if not line.endswith(b"\n"):
                        return events, offset
                    offset = next_offset
                    continue
                if isinstance(obj, dict):
                    events.append(obj)
                offset = next_offset
    except OSError:
        return [], offset


def parse_log_object(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize one Claude Code JSONL object into public structured events."""

    normalized: list[dict[str, Any]] = []
    base = _base_fields(obj)
    obj_type = obj.get("type")

    if obj_type == "permission-mode":
        normalized.append(
            {**base, "type": "permission_mode", "permission_mode": obj.get("permissionMode")}
        )
        return normalized

    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = message.get("role")
    content = message.get("content")

    if role == "assistant":
        for item in _content_items(content):
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text")
                if isinstance(text, str) and text:
                    normalized.append({**base, "type": "assistant_text", "text": text})
            elif item_type == "tool_use":
                name = item.get("name")
                input_value = item.get("input") if isinstance(item.get("input"), dict) else {}
                event = {
                    **base,
                    "type": "tool_use",
                    "name": name,
                    "input": redact_tool_input(input_value),
                    "tool_use_id": item.get("id"),
                }
                normalized.append(event)
                if name == "ExitPlanMode":
                    normalized.append(
                        {
                            **base,
                            "type": "plan",
                            "plan": input_value.get("plan"),
                            "plan_file": input_value.get("planFilePath"),
                            "tool_use_id": item.get("id"),
                        }
                    )
        usage = message.get("usage")
        if isinstance(usage, dict) or message.get("model") or message.get("stop_reason"):
            normalized.append(
                {
                    **base,
                    "type": "usage",
                    "usage": usage if isinstance(usage, dict) else {},
                    "model": message.get("model"),
                    "stop_reason": message.get("stop_reason"),
                }
            )
    elif role == "user":
        for item in _content_items(content):
            if item.get("type") == "tool_result":
                normalized.append(
                    {
                        **base,
                        "type": "tool_result",
                        "tool_use_id": item.get("tool_use_id"),
                        "content": _stringify_content(item.get("content")),
                    }
                )

    return normalized


def parse_log_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for obj in objects:
        events.extend(parse_log_object(obj))
    return events


def extract_final_assistant_text(events: list[dict[str, Any]]) -> str:
    parts = [
        str(event.get("text"))
        for event in events
        if event.get("type") == "assistant_text" and event.get("text")
    ]
    return "\n".join(parts).strip()


def redact_tool_input(value: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"content", "new_string", "old_string"} and isinstance(item, str):
            redacted[key] = f"<redacted:{len(item)} chars>"
        elif key in {"edits"} and isinstance(item, list):
            redacted[key] = [_redact_nested_edit(edit) for edit in item]
        else:
            redacted[key] = item
    return redacted


def _redact_nested_edit(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return redact_tool_input(value)


def _base_fields(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        key: obj.get(key)
        for key in ("uuid", "timestamp", "sessionId")
        if obj.get(key) is not None
    } | ({"session_id": obj.get("sessionId")} if obj.get("sessionId") is not None else {})


def _content_items(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)
