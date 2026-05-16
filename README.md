# cc-tmux

![Animated cc-tmux terminal demo](assets/cc-tmux-demo.svg)

Prefer an interactive terminal recording? Install [asciinema](https://asciinema.org/) and run:

```bash
asciinema play assets/cc-tmux-demo.cast
```

You can also convert the cast to GIF/SVG with tools such as `agg` if you need an embeddable artifact.

`cc-tmux` is a dependency-light Python CLI for running [Claude Code](https://claude.ai/code) inside a tmux session and controlling it from another process. It is designed for developers and AI orchestrators that need a reliable, inspectable way to start Claude, send prompts, capture progress, and stop the session without scraping a fragile GUI.

## Why tmux Control Mode?

The core workflow is intentionally simple:

- Claude Code runs in a normal tmux pane inside your project directory.
- `cc-tmux` sends instructions with `tmux send-keys`.
- Humans and bots inspect state with `tmux capture-pane`.
- tmux remains the source of truth, so you can attach manually whenever automation gets confusing.

This is robust across SSH, local terminals, servers, Telegram/Slack bots, and multi-agent systems. It avoids shell injection by using `subprocess` argv lists for tmux calls and only quotes the Claude launch command at tmux's required single-command boundary.

## Requirements

- Python 3.10+
- tmux 3.x recommended
- Claude Code CLI available as `claude` on `PATH`

## Install

From a checkout:

```bash
git clone git@github.com:cycorld/cc-tmux.git
cd cc-tmux
python -m pip install -e .
```

For development:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Quickstart

```bash
# Start Claude Code in a project. Session name is deterministic if omitted.
cc-tmux start ~/projects/my-app --prompt "Read the repo and summarize the test strategy."

# Send another instruction later.
cc-tmux send ~/projects/my-app "Implement the smallest safe fix and run tests."

# See recent pane output and the prompt-done heuristic.
cc-tmux status ~/projects/my-app

# Capture more transcript lines.
cc-tmux capture ~/projects/my-app -n 120

# Stop gracefully, falling back to kill if needed.
cc-tmux stop ~/projects/my-app --fallback-kill
```

## Command reference

### `cc-tmux start <project_path>`

Creates or reuses a tmux session running Claude Code in `project_path`.

Options:

- `--name NAME`: explicit tmux session name. Names are normalized with a `cc-tmux-` prefix.
- `--prompt TEXT`: prompt to send after startup.
- `--permission-mode MODE`: passed to Claude Code; defaults to `acceptEdits`.
- `--claude-arg ARG`: extra argument for Claude Code. Repeat for multiple args.
- `--auto-trust` / `--no-auto-trust`: send `Enter` after startup for workspace trust prompts. Default: enabled.

Example:

```bash
cc-tmux start . --name api-fix --permission-mode acceptEdits --claude-arg --model --claude-arg sonnet
```

### `cc-tmux send <session_or_project> "prompt"`

Sends text plus `Enter` to the Claude pane.

```bash
cc-tmux send api-fix "Run pytest and fix failures."
```

### `cc-tmux status <session_or_project> [--json]`

Reports whether the tmux session exists, whether the pane appears ready for a prompt, and the latest capture snippet. JSON output includes both `done` (kept for compatibility) and `last_prompt_ready` for the prompt-ready heuristic.

```bash
cc-tmux status . --json
```

### `cc-tmux capture <session_or_project>`

Prints captured pane output.

Options:

- `-n, --lines N`: number of recent lines, default 80.
- `--ansi` / `--no-ansi`: include or strip ANSI escapes. Default: no ANSI.

### `cc-tmux list [--json]`

Lists known sessions from `~/.local/state/cc-tmux/sessions.json` plus live tmux sessions with the `cc-tmux-` prefix.

### `cc-tmux stop <session_or_project>`

Sends `/exit` to Claude Code.

Options:

- `--kill`: immediately kill the tmux session.
- `--fallback-kill`: send `/exit`, wait, then kill if still live.
- `--wait SECONDS`: wait duration for graceful exit. Default: 3.

### `cc-tmux trust <session_or_project>`

Sends `Enter`, useful for accepting Claude Code's workspace trust prompt.

### `cc-tmux demo`

Creates a temporary workspace, starts Claude Code, asks it to create `CONTROL_MODE_RESULT.md`, polls for that file until `--wait`, and prints status plus `result_file_exists`, `result_file`, and a short preview when available. This requires live `tmux` and `claude`.

```bash
cc-tmux demo --wait 10
```

## State file

`cc-tmux` stores optional resolution metadata at:

```text
~/.local/state/cc-tmux/sessions.json
```

It maps project paths and session names to session records. tmux remains authoritative; deleting the state file is safe.

## Safety model

- Core tmux invocations use `subprocess.run([...], shell=False)`.
- Session names are normalized to safe tmux-friendly names.
- `--permission-mode acceptEdits` is the practical default for automation but still delegates edit behavior to Claude Code.
- `cc-tmux stop` is graceful by default. Use `--kill` only when you intentionally want to terminate tmux.
- Prompts are sent literally as tmux key strings. Avoid sending secrets unless you trust the tmux host and scrollback.

## Troubleshooting

- `tmux binary not found`: install tmux and ensure it is on `PATH`.
- `claude binary not found`: install Claude Code and verify `claude --version` works in the same shell.
- Workspace trust prompt blocks progress: run `cc-tmux trust <session>` or use the default `--auto-trust` behavior.
- Status says `exists: false`: check `tmux list-sessions` and `cc-tmux list --json`.
- ANSI-heavy output: prefer `cc-tmux capture --no-ansi`; control-mode streams can be noisy.
- Need manual recovery: `tmux attach -t cc-tmux-your-session`.

## Examples for chat/agent orchestration

### Hermes / Telegram bot

```bash
cc-tmux start /srv/repos/app --name app-worker --prompt "Inspect the latest failure."
cc-tmux status app-worker --json
cc-tmux send app-worker "Apply the fix and summarize changed files."
```

Return `cc-tmux capture app-worker -n 80` to the chat surface when the user asks for progress.

### Slack worker

A Slack command handler can resolve the channel to a stable session name:

```bash
cc-tmux start /srv/repos/app --name slack-C123456
cc-tmux send slack-C123456 "$SLACK_TEXT"
```

### Generic multi-agent supervisor

```bash
SESSION=$(cc-tmux start "$PROJECT" --prompt "$TASK" | awk '/session:/ {print $3}')
cc-tmux status "$SESSION" --json
```

If you need fully structured output, call `cc-tmux list --json` and `cc-tmux status --json` from your agent runtime.

## Battle-tested scenarios

Local battle testing covered:

- Initial `start --prompt` creating a file in a project.
- Follow-up `send` creating a second file.
- Resolution by project path, `list`, and `capture --no-ansi`.
- Graceful `stop` with fallback kill.
- Clean failure for invalid project paths.
- End-to-end `demo` against live `tmux` and `claude`.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest
```

CI runs Ruff and pytest on Python 3.10, 3.11, and 3.12.

## License

MIT
