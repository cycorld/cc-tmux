from __future__ import annotations

import pytest

from cc_tmux.tmux import CCTmuxError, Tmux, claude_command, normalize_session_name, prompt_done_heuristic, resolve_session, slugify_project


class FakeTmux(Tmux):
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live or set()
        self.calls: list[list[str]] = []

    def run(self, args: list[str], *, check: bool = True, text: bool = True):  # pragma: no cover - not used
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
