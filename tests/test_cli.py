from __future__ import annotations

from cc_tmux.cli import build_parser, main, wait_for_demo_result


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
