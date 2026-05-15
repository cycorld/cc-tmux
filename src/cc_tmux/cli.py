from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .state import SessionRecord, remove_record, upsert_record
from .tmux import (
    CCTmuxError,
    Tmux,
    claude_command,
    known_records,
    normalize_session_name,
    prompt_done_heuristic,
    resolve_session,
    slugify_project,
)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_start(args: argparse.Namespace) -> int:
    project = Path(args.project_path).expanduser().resolve()
    if not project.exists():
        raise CCTmuxError(f"project path does not exist: {project}")
    if not project.is_dir():
        raise CCTmuxError(f"project path is not a directory: {project}")

    tmux = Tmux()
    tmux.require()
    session = normalize_session_name(args.name) if args.name else slugify_project(project)
    command = claude_command(args.permission_mode, args.claude_arg)

    if tmux.has_session(session):
        created = False
    else:
        if shutil.which("claude") is None:
            raise CCTmuxError("claude binary not found on PATH")
        tmux.new_session(session, project, command)
        created = True
        wait_for_pane_ready(tmux, session, timeout=args.startup_timeout)

    upsert_record(SessionRecord.create(project, session))

    if args.auto_trust:
        tmux.send_keys(f"{session}:0.0", "Enter")
        time.sleep(args.trust_delay)
    if args.prompt:
        tmux.send_text(f"{session}:0.0", args.prompt)

    print(f"{'created' if created else 'using'} session: {session}")
    print(f"project: {project}")
    return 0


def wait_for_pane_ready(tmux: Tmux, session: str, *, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        capture = tmux.capture(f"{session}:0.0", lines=30)
        if capture.strip():
            return
        time.sleep(0.25)


def cmd_send(args: argparse.Namespace) -> int:
    tmux = Tmux()
    session = resolve_session(args.session_or_project, tmux)
    tmux.send_text(f"{session}:0.0", args.prompt)
    print(f"sent prompt to {session}")
    return 0


def _status_payload(identifier: str) -> dict[str, object]:
    tmux = Tmux()
    try:
        session = resolve_session(identifier, tmux)
    except CCTmuxError:
        session = identifier if identifier.startswith("cc-tmux-") else f"cc-tmux-{identifier}"
        return {
            "identifier": identifier,
            "session": session,
            "exists": False,
            "done": False,
            "capture": "",
        }
    exists = tmux.has_session(session)
    capture = tmux.capture(f"{session}:0.0", lines=80) if exists else ""
    return {
        "identifier": identifier,
        "session": session,
        "exists": exists,
        "done": prompt_done_heuristic(capture),
        "capture": capture,
    }


def cmd_status(args: argparse.Namespace) -> int:
    payload = _status_payload(args.session_or_project)
    if args.json:
        _print_json(payload)
    else:
        print(f"session: {payload['session']}")
        print(f"exists: {payload['exists']}")
        print(f"prompt_done: {payload['done']}")
        capture = str(payload.get("capture") or "").rstrip()
        if capture:
            print("--- capture ---")
            print(capture)
    return 0 if payload["exists"] else 1


def cmd_capture(args: argparse.Namespace) -> int:
    tmux = Tmux()
    session = resolve_session(args.session_or_project, tmux)
    print(tmux.capture(f"{session}:0.0", lines=args.lines, ansi=args.ansi), end="")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    tmux = Tmux()
    live = set(tmux.list_sessions())
    records = known_records()
    payload = []
    seen: set[str] = set()
    for record in records:
        if not record.session_name.startswith("cc-tmux-"):
            continue
        payload.append({
            "session": record.session_name,
            "project_path": record.project_path,
            "created_at": record.created_at,
            "exists": record.session_name in live,
        })
        seen.add(record.session_name)
    live_untracked = (
        name for name in live if name.startswith("cc-tmux-") and name not in seen
    )
    for session in sorted(live_untracked):
        payload.append({
            "session": session,
            "project_path": None,
            "created_at": None,
            "exists": True,
        })
    if args.json:
        _print_json(payload)
    else:
        if not payload:
            print("no cc-tmux sessions found")
        for item in payload:
            state = "live" if item["exists"] else "stopped"
            print(f"{item['session']}\t{state}\t{item.get('project_path') or ''}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    tmux = Tmux()
    session = resolve_session(args.session_or_project, tmux)
    if args.kill:
        tmux.kill_session(session)
        remove_record(session)
        print(f"killed {session}")
        return 0
    tmux.send_keys(f"{session}:0.0", "/exit", "Enter")
    if args.wait:
        deadline = time.time() + args.wait
        while time.time() < deadline:
            if not tmux.has_session(session):
                remove_record(session)
                print(f"stopped {session}")
                return 0
            time.sleep(0.5)
    if args.fallback_kill and tmux.has_session(session):
        tmux.kill_session(session)
        remove_record(session)
        print(f"sent /exit then killed {session}")
    else:
        print(f"sent /exit to {session}")
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    tmux = Tmux()
    session = resolve_session(args.session_or_project, tmux)
    tmux.send_keys(f"{session}:0.0", "Enter")
    print(f"sent Enter to {session}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="cc-tmux-demo-"))
    prompt = (
        "Create a file named CONTROL_MODE_RESULT.md with a short note saying "
        "cc-tmux controlled this Claude Code session through tmux. Then stop."
    )
    print(f"demo workspace: {workspace}")
    start_args = argparse.Namespace(
        project_path=str(workspace),
        name=args.name or f"demo-{workspace.name.rsplit('-', 1)[-1]}",
        prompt=prompt,
        permission_mode=args.permission_mode,
        claude_arg=[],
        auto_trust=True,
        startup_timeout=15.0,
        trust_delay=1.0,
    )
    cmd_start(start_args)
    print("waiting briefly for Claude Code to begin...")
    time.sleep(args.wait)
    status = _status_payload(normalize_session_name(start_args.name))
    _print_json({k: v for k, v in status.items() if k != "capture"})
    print("Run `cc-tmux capture SESSION` for the transcript or inspect:", workspace)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cc-tmux", description="Orchestrate Claude Code in tmux.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="create or reuse a tmux session running Claude Code")
    p.add_argument("project_path")
    p.add_argument("--name")
    p.add_argument("--prompt")
    p.add_argument("--permission-mode", default="acceptEdits")
    p.add_argument(
        "--claude-arg",
        action="append",
        default=[],
        help="extra argument passed to claude; repeatable",
    )
    p.add_argument("--auto-trust", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--startup-timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    p.add_argument("--trust-delay", type=float, default=1.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("send", help="send a prompt to a running session")
    p.add_argument("session_or_project")
    p.add_argument("prompt")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("status", help="show session state and recent pane capture")
    p.add_argument("session_or_project")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("capture", help="print captured pane output")
    p.add_argument("session_or_project")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("--ansi", dest="ansi", action="store_true", default=False)
    p.add_argument("--no-ansi", dest="ansi", action="store_false")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("list", help="list known/live cc-tmux sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stop", help="ask Claude Code to exit, optionally killing tmux")
    p.add_argument("session_or_project")
    p.add_argument("--kill", action="store_true", help="kill the tmux session immediately")
    p.add_argument(
        "--fallback-kill",
        action="store_true",
        help="kill if /exit does not stop within --wait",
    )
    p.add_argument("--wait", type=float, default=3.0)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("trust", help="send Enter for Claude Code workspace trust prompt")
    p.add_argument("session_or_project")
    p.set_defaults(func=cmd_trust)

    p = sub.add_parser("demo", help="run a safe temporary workspace demo")
    p.add_argument("--name")
    p.add_argument("--permission-mode", default="acceptEdits")
    p.add_argument("--wait", type=float, default=5.0)
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CCTmuxError as exc:
        print(f"cc-tmux: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
