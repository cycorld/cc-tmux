from __future__ import annotations

import json
from types import SimpleNamespace

import cc_tmux.cli as cli
from cc_tmux.cli import build_parser, main, wait_for_demo_result
from cc_tmux.tmux import CCTmuxError


class FakeCliTmux:
    live = {"cc-tmux-demo"}
    sent_keys: list[tuple[str, tuple[str, ...]]] = []
    captures: list[str] = ["│ ❯ "]

    def has_session(self, session_name):
        return session_name in self.live

    def send_keys(self, target, *keys):
        self.sent_keys.append((target, keys))

    def capture(self, target, lines=80, ansi=False):
        return self.captures.pop(0) if self.captures else ""


def test_parser_start_defaults():
    parser = build_parser()
    args = parser.parse_args(["start", "/tmp/project"])
    assert args.permission_mode == "acceptEdits"
    assert args.auto_trust is True
    assert args.claude_arg == []


def test_parser_key_accepts_multiple_keys():
    parser = build_parser()
    args = parser.parse_args(["key", "demo", "Escape", "C-c", "Enter"])
    assert args.session_or_project == "demo"
    assert args.keys == ["Escape", "C-c", "Enter"]


def test_parser_interrupt_defaults_to_escape_and_cleanup():
    parser = build_parser()
    args = parser.parse_args(["interrupt", "demo"])
    assert args.key == "Escape"
    assert args.wait_ready is None
    assert args.clear_input is True
    assert args.settle == 0.3


def test_parser_interrupt_can_disable_clear_and_settle():
    parser = build_parser()
    args = parser.parse_args(["interrupt", "demo", "--no-clear-input", "--settle", "0"])
    assert args.clear_input is False
    assert args.settle == 0


def test_parser_serve_defaults():
    parser = build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 19410
    assert args.api_key is None
    assert args.api_key_env == "CC_TMUX_API_KEY"


def test_parser_service_install_defaults():
    parser = build_parser()
    args = parser.parse_args(["service", "install"])
    assert args.name == "cc-tmux-api"
    assert args.host == "127.0.0.1"
    assert args.port == 19410
    assert args.api_key_env == "CC_TMUX_API_KEY"
    assert args.env_file == "%h/.config/cc-tmux/server.env"
    assert args.start is False


def test_service_unit_content():
    unit = cli.build_service_unit(
        name="cc-tmux-api",
        host="127.0.0.1",
        port=19410,
        api_key_env="CC_TMUX_API_KEY",
        env_file="%h/.config/cc-tmux/server.env",
        executable="/usr/local/bin/cc-tmux",
    )

    assert "EnvironmentFile=-%h/.config/cc-tmux/server.env" in unit
    assert (
        "ExecStart=/usr/local/bin/cc-tmux serve --host 127.0.0.1 --port 19410 "
        "--api-key-env CC_TMUX_API_KEY"
    ) in unit
    assert "Restart=on-failure" in unit


def test_service_install_writes_unit_and_reloads(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        cli, "_run_systemctl", lambda *args: calls.append(args) or SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(cli, "_service_exec", lambda: "/tmp/bin/cc-tmux")

    exit_code = main(["service", "install", "--host", "0.0.0.0", "--port", "19999"])

    unit_path = tmp_path / "config" / "systemd" / "user" / "cc-tmux-api.service"
    assert exit_code == 0
    assert unit_path.exists()
    unit = unit_path.read_text()
    assert "ExecStart=/tmp/bin/cc-tmux serve --host 0.0.0.0 --port 19999" in unit
    assert calls == [("daemon-reload",)]
    assert "installed:" in capsys.readouterr().out


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
    assert payload["plan_mode"] is False
    assert payload["awaiting_plan_approval"] is False
    assert payload["plan_file"] is None


def test_key_command_sends_tmux_keys_without_shell(monkeypatch, capsys):
    FakeCliTmux.sent_keys = []
    monkeypatch.setattr(cli, "Tmux", FakeCliTmux)

    exit_code = main(["key", "demo", "Escape", "C-c", "Enter"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeCliTmux.sent_keys == [("cc-tmux-demo:0.0", ("Escape", "C-c", "Enter"))]
    assert "sent keys to cc-tmux-demo: Escape C-c Enter" in captured.out


def test_interrupt_sends_default_escape_waits_ready_clears_and_settles(monkeypatch, capsys):
    FakeCliTmux.sent_keys = []
    FakeCliTmux.captures = ["working...", "│ ❯ "]
    sleeps = []
    monkeypatch.setattr(cli, "Tmux", FakeCliTmux)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: sleeps.append(seconds))

    exit_code = main(["interrupt", "demo", "--wait-ready", "1"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeCliTmux.sent_keys == [
        ("cc-tmux-demo:0.0", ("Escape",)),
        ("cc-tmux-demo:0.0", ("C-u",)),
    ]
    assert sleeps[-1] == 0.3
    assert "sent interrupt key to cc-tmux-demo: Escape" in captured.out
    assert "last_prompt_ready: true" in captured.out
    assert "cleared input line for cc-tmux-demo: C-u" in captured.out


def test_interrupt_can_use_custom_key(monkeypatch, capsys):
    FakeCliTmux.sent_keys = []
    monkeypatch.setattr(cli, "Tmux", FakeCliTmux)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    exit_code = main(["interrupt", "demo", "--key", "C-c"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeCliTmux.sent_keys == [
        ("cc-tmux-demo:0.0", ("C-c",)),
        ("cc-tmux-demo:0.0", ("C-u",)),
    ]
    assert "sent interrupt key to cc-tmux-demo: C-c" in captured.out


def test_interrupt_can_skip_clear_and_settle(monkeypatch, capsys):
    FakeCliTmux.sent_keys = []
    monkeypatch.setattr(cli, "Tmux", FakeCliTmux)

    exit_code = main(["interrupt", "demo", "--no-clear-input", "--settle", "0"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert FakeCliTmux.sent_keys == [("cc-tmux-demo:0.0", ("Escape",))]
    assert "cleared input line" not in captured.out
