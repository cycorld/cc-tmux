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

Interrupt a busy turn before changing direction. This is the recommended follow-up workflow: `interrupt` waits for readiness, clears stale input with `C-u`, and settles briefly by default before returning so the next `send` is not appended to the old line or missing its first character.

```bash
cc-tmux interrupt project-worker --wait-ready 10
cc-tmux send project-worker "New instruction after the interrupted turn."
```

Send TUI keys such as `Escape`, `C-c`, or `Enter` when you need overlay/control behavior:

```bash
cc-tmux key project-worker Escape
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
3. Poll `cc-tmux status --json` for `exists`; use `last_prompt_ready` (or legacy `done`) as the prompt-ready heuristic. For planning flows, also watch `plan_mode`, `awaiting_plan_approval`, and `plan_file`.
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
- If Claude is busy and the operator needs to redirect it, run `cc-tmux interrupt hermes-app --wait-ready 10` first, then send the follow-up with `cc-tmux send`. This default interrupt flow clears stale input and briefly settles before returning; sending follow-ups without it can append text to an old input line or lose the first character.
- Close Claude Code overlays and side panels with `cc-tmux key hermes-app Escape`.
- Capture output is for status, not a formal API contract.

## Server mode operator guidance

Use `cc-tmux serve` when a supervisor needs an HTTP API rather than shelling out for each action. Install optional server dependencies and run on localhost by default:

```bash
python -m pip install -e '/path/to/cc-tmux[server]'
cc-tmux serve --host 127.0.0.1 --port 19410
```

Endpoints mirror the CLI:

- `GET /health`: readiness check.
- `POST /v1/sessions`: start/reuse a Claude Code tmux worker with `project_path`, optional `name`, `prompt`, `permission_mode`, and `auto_trust`.
- `GET /v1/sessions`, `GET /v1/sessions/{id}/status`, `GET /v1/sessions/{id}/capture?n=120`: inspect state and transcript tail.
- `POST /v1/sessions/{id}/messages`: send a prompt, optionally `wait_ready` until the prompt returns.
- `POST /v1/sessions/{id}/interrupt` and `/key`: recover or operate focused TUI controls.
- `DELETE /v1/sessions/{id}`: ask Claude to exit and kill the tmux session if it remains live.
- `POST /v1/chat/completions`: minimal OpenAI-compatible non-streaming wrapper. Put `project_path`, `session`, `permission_mode`, `wait_ready`, or `timeout_seconds` in `metadata`.

Server caveats: no built-in authentication, no streaming yet (`stream=true` returns `501`), synchronous request handling, and OpenAI-style assistant content is a cleaned transcript tail. Keep it bound to `127.0.0.1` or protect it with an external auth/reverse proxy.

## Plan mode operator guidance

Use plan mode when the supervisor wants Claude to propose steps without changing files yet. Live testing confirmed `/plan ...` and `--permission-mode plan` show a plan/approval flow and do not create the requested file before approval.

Start directly in plan mode:

```bash
cc-tmux start . --name planner --permission-mode plan --prompt "Plan adding FEATURE.md, do not implement"
```

Or ask an existing worker for a plan:

```bash
cc-tmux send planner "/plan Create a step-by-step plan for the requested change, but do not implement."
cc-tmux status planner --json
```

Operational signals:

- `plan_mode`: true when capture shows `plan mode on`, `Enabled plan mode`, or the plan approval screen.
- `awaiting_plan_approval`: true when Claude is at `Ready to code?` / `Would you like to proceed?`.
- `plan_file`: a visible `~/.claude/plans/<name>.md` path, or null.

Approval caution:

- Inspect `cc-tmux capture planner -n 120` before sending keys.
- If option `1. Yes, auto-accept edits` is selected and you intend to implement, `cc-tmux key planner Enter` approves it.
- To revise the plan, choose option `4` or send feedback carefully; blind free-form sends may be interpreted by the focused approval UI rather than as a normal prompt.

## Claude Code slash commands and overlays

Claude Code slash commands can be driven through `cc-tmux send`:

```bash
cc-tmux send project-worker "/btw Is there a simpler approach? Answer briefly."
```

Operator guidance:

- Use `/btw <question>` for a side question while preserving the main task context. Live testing showed `/btw What is 2+2? answer in one short sentence.` produced `2+2 equals 4.`
- `/btw` and other slash commands may open overlays with controls such as `Esc to close`; close them with `cc-tmux key SESSION Escape` before continuing automated sends.
- `/loop` loads/starts the looping skill and can lead to durable or autonomous repeated actions. Use it only intentionally, with explicit prompt and interval instructions, and close test overlays with `cc-tmux key SESSION Escape`.
- `/plan <request>` enters plan mode and can stop at the approval UI. Use the plan-mode status fields before approving or revising.
- `/help` is useful for discovering the currently available command surface.

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
