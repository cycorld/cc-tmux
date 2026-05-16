# cc-tmux Agent Skill

Use `cc-tmux` when an AI agent needs to run Claude Code as a durable worker in a tmux session, send it instructions, and inspect progress through captured terminal output.

## Core mental model

- One tmux session per project/task.
- Claude Code runs in pane `SESSION:0.0`.
- Prompts are sent with `cc-tmux send`.
- Progress is inspected with `cc-tmux status` or `cc-tmux capture`.
- Humans can always attach with `tmux attach -t SESSION`.

## Universal setup

```bash
python -m pip install -e /path/to/cc-tmux
cc-tmux --help
claude --version
tmux -V
```

Start a worker:

```bash
cc-tmux start /path/to/project --name project-worker --prompt "Read the repository and wait for instructions."
```

Send work:

```bash
cc-tmux send project-worker "Implement the requested change, run tests, and summarize results."
```

Check progress:

```bash
cc-tmux status project-worker --json
cc-tmux capture project-worker -n 120
```

Stop:

```bash
cc-tmux stop project-worker --fallback-kill
```

## Hermes setup

Hermes agents can call the CLI from shell tools. Recommended pattern:

1. Start a named session per repository or user task.
2. Store the session name in the parent task state.
3. Poll `cc-tmux status --json` for `exists`; use `last_prompt_ready` (or legacy `done`) as the prompt-ready heuristic.
4. Use `cc-tmux capture -n 80` for summaries sent back to Telegram.

Example:

```bash
cc-tmux start /home/cycorld/projects/app --name hermes-app --prompt "Investigate the failing CI job."
cc-tmux status hermes-app --json
```

For a self-check on a machine with live `tmux` and `claude`, run `cc-tmux demo --wait 30`. The demo now reports `result_file_exists` and `result_file` for `CONTROL_MODE_RESULT.md` in addition to the pane prompt-ready heuristic, so a completed file is visible even if the terminal prompt heuristic is conservative.

Pitfalls:

- Do not assume `/workspace`; pass the real repository path.
- If Claude shows a trust prompt, run `cc-tmux trust hermes-app`.
- Capture output is for status, not a formal API contract.

## Claude Code as the controlled worker

`cc-tmux` launches:

```bash
claude --permission-mode acceptEdits
```

by default. Override if you need a stricter mode:

```bash
cc-tmux start . --permission-mode default
```

Pass additional Claude args by repeating `--claude-arg`:

```bash
cc-tmux start . --claude-arg --model --claude-arg sonnet
```

## Codex, Gemini, or generic agents as supervisors

Codex/Gemini-style agents should treat `cc-tmux` as an external worker API:

- Use deterministic `--name` values.
- Never parse raw ANSI unless you requested `--ansi`.
- Prefer JSON commands for machine state: `list --json`, `status --json`.
- Keep prompts explicit and idempotent.

Recipe:

```bash
cc-tmux start "$PROJECT" --name "$TASK_SESSION" --prompt "$INITIAL_INSTRUCTION"
cc-tmux send "$TASK_SESSION" "$FOLLOWUP"
cc-tmux capture "$TASK_SESSION" -n 60 --no-ansi
```

## Telegram and Slack surfaces

Chat platforms are good supervisors because tmux preserves context between messages.

Suggested mapping:

- Telegram chat id + repo slug -> `cc-tmux-tg-<chat>-<repo>`
- Slack channel id + repo slug -> `cc-tmux-slack-<channel>-<repo>`

Command handler flow:

```bash
cc-tmux start /srv/repos/my-app --name tg-123-my-app
cc-tmux send tg-123-my-app "$USER_MESSAGE"
cc-tmux status tg-123-my-app --json
```

Send recent output back to chat:

```bash
cc-tmux capture tg-123-my-app -n 80
```

Pitfalls:

- Telegram Markdown may interpret terminal characters; wrap captures in code blocks.
- Slack messages may exceed length limits; truncate captures.
- Do not send secrets into prompts unless the tmux host and bot logs are trusted.

## macOS

Install dependencies:

```bash
brew install tmux python
# Install Claude Code per Anthropic docs, then:
python -m pip install -e /path/to/cc-tmux
```

Notes:

- Terminal/iTerm permission prompts can affect automation.
- Keep long-running sessions in a stable terminal or launch daemon environment.

## Linux

Install dependencies:

```bash
sudo apt-get update
sudo apt-get install -y tmux python3 python3-pip
python3 -m pip install -e /path/to/cc-tmux
```

Notes:

- Server usage over SSH is a primary use case.
- Use `tmux attach -t SESSION` for manual inspection.

## WSL

Use Linux-side paths and tools:

```bash
sudo apt-get install -y tmux python3 python3-pip
python3 -m pip install -e /home/you/cc-tmux
cc-tmux start /home/you/project
```

Pitfalls:

- Avoid mixing Windows paths with Linux tmux sessions.
- Ensure `claude` is installed in WSL, not only in Windows.

## Battle-test notes

Recent local battle testing covered: initial `start --prompt`, follow-up `send`, project-path resolution, `list`, `capture --no-ansi`, invalid path errors, fallback `stop`, and the live `demo` workflow. Treat `status --json` as a lightweight operational signal rather than proof of task completion; for demos or file-producing tasks, also check the expected filesystem artifact.

A deterministic asciinema cast is included in the repository:

```bash
asciinema play assets/cc-tmux-demo.cast
```

## Operational checklist

Before delegating a task:

- `tmux -V` works.
- `claude --version` works.
- The project path exists.
- You chose a stable session name.
- You know whether `acceptEdits` is appropriate.

During work:

- Poll `cc-tmux status --json`.
- Use `cc-tmux capture -n 80` for human summaries.
- Send follow-ups with `cc-tmux send`.

After work:

- Ask Claude to summarize changed files and test results.
- Stop with `cc-tmux stop --fallback-kill` if the session is no longer needed.
