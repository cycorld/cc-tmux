from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .state import SessionRecord, load_state

_PREFIX = "cc-tmux-"
_DONE_MARKERS = (
    "Human:",
    "Try ",
    "Bash(",
    "Would you like",
    "Do you trust",
    "│ >",
    "> ",
)


class CCTmuxError(RuntimeError):
    """Raised for expected cc-tmux operational failures."""


@dataclass(slots=True)
class Completed:
    args: list[str]
    stdout: str
    stderr: str
    returncode: int


class Tmux:
    def __init__(self, tmux_bin: str = "tmux") -> None:
        self.tmux_bin = tmux_bin

    def require(self) -> None:
        if shutil.which(self.tmux_bin) is None:
            raise CCTmuxError(f"tmux binary not found on PATH: {self.tmux_bin}")

    def run(self, args: list[str], *, check: bool = True, text: bool = True) -> Completed:
        cmd = [self.tmux_bin, *args]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=text)
        result = Completed(cmd, proc.stdout or "", proc.stderr or "", proc.returncode)
        if check and proc.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CCTmuxError(f"tmux command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
        return result

    def has_session(self, session_name: str) -> bool:
        return self.run(["has-session", "-t", session_name], check=False).returncode == 0

    def new_session(self, session_name: str, cwd: Path, command: list[str]) -> None:
        # tmux new-session accepts one command string. Use tmux's argv for safety around tmux options
        # and quote the program command with shlex only at this boundary.
        import shlex

        command_string = " ".join(shlex.quote(part) for part in command)
        self.run(["new-session", "-d", "-s", session_name, "-c", str(cwd), command_string])

    def send_keys(self, target: str, *keys: str) -> None:
        self.run(["send-keys", "-t", target, *keys])

    def capture(self, target: str, lines: int = 80, ansi: bool = False) -> str:
        args = ["capture-pane", "-t", target, "-p", "-S", f"-{lines}"]
        if ansi:
            args.insert(1, "-e")
        return self.run(args).stdout

    def list_sessions(self) -> list[str]:
        result = self.run(["list-sessions", "-F", "#{session_name}"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def kill_session(self, session_name: str) -> None:
        self.run(["kill-session", "-t", session_name])


def slugify_project(path: str | Path, *, prefix: str = _PREFIX, max_length: int = 80) -> str:
    resolved = Path(path).expanduser().resolve()
    raw = f"{resolved.name}-{abs(hash(str(resolved))) & 0xFFFFF:x}"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.").lower()
    if not slug:
        slug = "project"
    full = f"{prefix}{slug}"
    return full[:max_length].rstrip("-.")


def normalize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", name.strip()).strip("-.")
    if not cleaned:
        raise CCTmuxError("session name cannot be empty")
    return cleaned if cleaned.startswith(_PREFIX) else f"{_PREFIX}{cleaned}"


def claude_command(permission_mode: str | None, claude_args: list[str] | None = None) -> list[str]:
    cmd = ["claude"]
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    if claude_args:
        cmd.extend(claude_args)
    return cmd


def resolve_session(identifier: str, tmux: Tmux | None = None) -> str:
    records = load_state()
    if identifier in records:
        return records[identifier].session_name

    path = Path(identifier).expanduser()
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    if resolved in records:
        return records[resolved].session_name

    candidates = [identifier]
    if not identifier.startswith(_PREFIX):
        candidates.append(f"{_PREFIX}{identifier}")
        if path.exists():
            candidates.append(slugify_project(path))

    client = tmux or Tmux()
    for candidate in candidates:
        if client.has_session(candidate):
            return candidate
    raise CCTmuxError(f"could not resolve cc-tmux session or project: {identifier}")


def prompt_done_heuristic(capture: str) -> bool:
    stripped = capture.strip()
    if not stripped:
        return False
    tail = "\n".join(stripped.splitlines()[-8:])
    return any(marker in tail for marker in _DONE_MARKERS)


def known_records() -> list[SessionRecord]:
    dedup: dict[str, SessionRecord] = {}
    for record in load_state().values():
        dedup[record.session_name] = record
    return list(dedup.values())
