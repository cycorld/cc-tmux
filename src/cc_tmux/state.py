from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "cc-tmux"
STATE_FILE = STATE_DIR / "sessions.json"


@dataclass(slots=True)
class SessionRecord:
    project_path: str
    session_name: str
    created_at: str

    @classmethod
    def create(cls, project_path: Path, session_name: str) -> "SessionRecord":
        return cls(
            project_path=str(project_path.resolve()),
            session_name=session_name,
            created_at=datetime.now(timezone.utc).isoformat(),
        )


def load_state(path: Path = STATE_FILE) -> dict[str, SessionRecord]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    records: dict[str, SessionRecord] = {}
    for key, value in data.items():
        try:
            records[key] = SessionRecord(**value)
        except TypeError:
            continue
    return records


def save_state(records: dict[str, SessionRecord], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {key: asdict(value) for key, value in sorted(records.items())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def upsert_record(record: SessionRecord, path: Path = STATE_FILE) -> None:
    records = load_state(path)
    records[record.session_name] = record
    records[record.project_path] = record
    save_state(records, path)


def remove_record(session_name: str, path: Path = STATE_FILE) -> None:
    records = load_state(path)
    to_remove = [key for key, value in records.items() if value.session_name == session_name or key == session_name]
    for key in to_remove:
        records.pop(key, None)
    save_state(records, path)
