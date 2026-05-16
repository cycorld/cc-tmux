from __future__ import annotations

import json

from cc_tmux.claude_logs import (
    extract_final_assistant_text,
    parse_log_object,
    project_log_dir,
    read_jsonl_events,
)


def test_project_log_dir_matches_observed_mapping(tmp_path):
    project = tmp_path / "foo bar"
    project.mkdir()

    log_dir = project_log_dir(project)

    assert log_dir.name == str(project.resolve()).replace("/", "-")
    assert log_dir.parent.name == "projects"


def test_parse_log_object_normalizes_assistant_tool_plan_usage_and_user_result():
    assistant = {
        "uuid": "u1",
        "timestamp": "2026-01-01T00:00:00Z",
        "sessionId": "s1",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "content": [
                {"type": "text", "text": "hello"},
                {
                    "type": "tool_use",
                    "id": "toolu_write",
                    "name": "Write",
                    "input": {"file_path": "README.md", "content": "secret text"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_plan",
                    "name": "ExitPlanMode",
                    "input": {"plan": "# Plan", "planFilePath": "/tmp/plan.md"},
                },
            ],
        },
    }
    user_result = {
        "uuid": "u2",
        "sessionId": "s1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_write",
                    "content": "File created successfully",
                }
            ],
        },
    }

    events = parse_log_object(assistant) + parse_log_object(user_result)

    assert events[0]["type"] == "assistant_text"
    assert events[0]["text"] == "hello"
    write = next(event for event in events if event.get("tool_use_id") == "toolu_write")
    assert write["type"] == "tool_use"
    assert write["input"]["file_path"] == "README.md"
    assert write["input"]["content"] == "<redacted:11 chars>"
    plan = next(event for event in events if event["type"] == "plan")
    assert plan["plan"] == "# Plan"
    assert plan["plan_file"] == "/tmp/plan.md"
    usage = next(event for event in events if event["type"] == "usage")
    assert usage["model"] == "claude-test"
    assert usage["usage"]["output_tokens"] == 2
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["content"] == "File created successfully"
    assert extract_final_assistant_text(events) == "hello"


def test_parse_permission_mode_and_read_jsonl_offset(tmp_path):
    log = tmp_path / "session.jsonl"
    first = {"type": "permission-mode", "permissionMode": "plan", "sessionId": "s1"}
    second = {"message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}
    log.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")

    objects, offset = read_jsonl_events(log, offset=0)
    again, again_offset = read_jsonl_events(log, offset=offset)

    assert [event["type"] for obj in objects for event in parse_log_object(obj)] == [
        "permission_mode",
        "assistant_text",
    ]
    assert parse_log_object(objects[0])[0]["permission_mode"] == "plan"
    assert again == []
    assert again_offset == offset
