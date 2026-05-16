from __future__ import annotations

import json

import cc_tmux.cli as cli
from cc_tmux.cli import build_parser, main, wait_for_demo_result
from cc_tmux.tmux import CCTmuxError


def test_parser_start_defaults():
    parser = build_parser()
    args = parser.parse_args(["start", "/tmp/project"])
    assert args.permission_mode == "acceptEdits"
    assert args.auto_trust is True
    assert args.claude_arg == []


def test_help_exits_success(capsys):
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    captured = capsys.readouterr()
    assert "Orchestrate Claude Code in tmux" in captured.out


def test_demo_result_poll_reports_existing_file(tmp_path):
    result = tmp_path / "CONTROL_MODE_RESULT.md"
    result.write_text("done")

    payload = wait_for_demo_result(result, timeout=0)

    assert payload["result_file_exists"] is True
    assert payload["result_wait_seconds"] >= 0


def test_demo_result_poll_times_out_for_missing_file(tmp_path):
    payload = wait_for_demo_result(tmp_path / "missing.md", timeout=0, interval=0.01)

    assert payload["result_file_exists"] is False
    assert payload["result_wait_seconds"] >= 0


def test_status_missing_session_json_exits_success(monkeypatch, capsys):
    def fake_resolve_session(identifier, tmux):
        raise CCTmuxError(f"missing: {identifier}")

    monkeypatch.setattr(cli, "resolve_session", fake_resolve_session)

    exit_code = main(["status", "stopped-session", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["identifier"] == "stopped-session"
    assert payload["session"] == "cc-tmux-stopped-session"
    assert payload["exists"] is False
    assert payload["done"] is False
    assert payload["last_prompt_ready"] is False
