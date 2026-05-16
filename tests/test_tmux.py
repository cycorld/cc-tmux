from __future__ import annotations

import pytest

from cc_tmux.tmux import (
    CCTmuxError,
    Tmux,
    awaiting_plan_approval_heuristic,
    claude_command,
    normalize_session_name,
    plan_file_heuristic,
    plan_mode_heuristic,
    prompt_done_heuristic,
    prompt_ready_heuristic,
    resolve_session,
    slugify_project,
)


class FakeTmux(Tmux):
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live or set()
        self.calls: list[list[str]] = []

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        text: bool = True,
    ):  # pragma: no cover - not used
        self.calls.append(args)
        raise AssertionError("run should not be called in these tests")

    def has_session(self, session_name: str) -> bool:
        self.calls.append(["has-session", "-t", session_name])
        return session_name in self.live


def test_slugify_project_is_deterministic_and_prefixed(tmp_path):
    project = tmp_path / "My Cool Project!"
    project.mkdir()
    first = slugify_project(project)
    second = slugify_project(project)
    assert first == second
    assert first.startswith("cc-tmux-my-cool-project-")
    assert " " not in first


def test_normalize_session_name():
    assert normalize_session_name("demo") == "cc-tmux-demo"
    assert normalize_session_name("cc-tmux-demo") == "cc-tmux-demo"
    assert normalize_session_name("demo name") == "cc-tmux-demo-name"
    with pytest.raises(CCTmuxError):
        normalize_session_name("   ")


def test_claude_command_constructs_argv_without_shell():
    assert claude_command("acceptEdits", ["--model", "sonnet"]) == [
        "claude",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "sonnet",
    ]
    assert claude_command(None, []) == ["claude"]


def test_send_text_uses_literal_mode_then_enter(monkeypatch):
    tmux = Tmux()
    calls = []

    def fake_run(args, *, check=True, text=True):
        calls.append(args)

    monkeypatch.setattr(tmux, "run", fake_run)
    tmux.send_text("session:0.0", "hello; rm -rf /")
    assert calls == [
        ["send-keys", "-t", "session:0.0", "-l", "hello; rm -rf /"],
        ["send-keys", "-t", "session:0.0", "Enter"],
    ]


def test_resolve_session_prefers_live_prefixed_name():
    tmux = FakeTmux({"cc-tmux-demo"})
    assert resolve_session("demo", tmux) == "cc-tmux-demo"
    assert ["has-session", "-t", "demo"] in tmux.calls
    assert ["has-session", "-t", "cc-tmux-demo"] in tmux.calls


def test_resolve_session_errors_for_unknown():
    with pytest.raises(CCTmuxError):
        resolve_session("missing", FakeTmux(set()))


def test_prompt_done_heuristic_detects_prompt_tail():
    assert prompt_done_heuristic("work...\n> ")
    assert prompt_done_heuristic("Do you trust the files in this folder?")
    assert not prompt_done_heuristic("")


def test_prompt_ready_heuristic_detects_claude_code_unicode_prompt():
    capture = "\n".join(
        [
            "✻ Welcome to Claude Code",
            "  Some prior output",
            "╭────────────────────────────────────────────────────────╮",
            "│ ❯ show me the file contents                            │",
            "╰────────────────────────────────────────────────────────╯",
        ]
    )

    assert prompt_ready_heuristic(capture)


def test_prompt_ready_heuristic_ignores_old_history_outside_tail():
    capture = "❯ historical prompt\n" + "\n".join(f"line {i}" for i in range(20))

    assert not prompt_ready_heuristic(capture)


def test_prompt_ready_heuristic_strips_ansi_and_detects_ascii_prompt():
    assert prompt_ready_heuristic("working\n\x1b[36m│ > \x1b[0m")
    assert not prompt_ready_heuristic("")


def test_plan_mode_heuristic_detects_status_bar_and_slash_command_entry():
    assert plan_mode_heuristic("⏸ plan mode on (shift+tab to cycle)")
    assert plan_mode_heuristic("⎿ Enabled plan mode\nReady to code?")
    assert not plan_mode_heuristic("work complete\n│ ❯ ")


def test_plan_approval_heuristic_detects_ready_to_code_screen():
    capture = "\n".join(
        [
            "Ready to code?",
            "Here is Claude's plan:",
            "Claude has written up a plan and is ready to execute. Would you like to proceed?",
            "1. Yes, auto-accept edits",
            "2. Yes, manually approve edits",
            "3. No, refine with Ultraplan on Claude Code on the web",
            "4. Tell Claude what to change",
        ]
    )

    assert plan_mode_heuristic(capture)
    assert awaiting_plan_approval_heuristic(capture)
    assert not awaiting_plan_approval_heuristic("Ready to code?\nDrafting plan...")


def test_plan_file_heuristic_extracts_visible_plan_path_and_strips_ansi():
    capture = "Saved to \x1b[36m~/.claude/plans/fluffy-wibbling-oasis.md\x1b[0m"

    assert plan_file_heuristic(capture) == "~/.claude/plans/fluffy-wibbling-oasis.md"
    assert plan_file_heuristic("no plan file") is None
