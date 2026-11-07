"""The raw log holds exactly the strings the agent saw, and get_work_record is never in it."""

import json

from swarm.env.database import TASK_TOOLS, TaskEnv
from swarm.llm.client import ScriptedClient, scripted_sequence, text_response, tool_use_response
from swarm.llm.loop import run_tool_loop
from swarm.llm.types import text_block, user_message

TAGS = {"phase": "task", "agent": "A", "episode": 1}
CALLS = [
    ("inspect_database", {}),
    ("query_database", {"sql": "SELECT COUNT(*) AS n FROM refunds"}),
    ("query_database", {"sql": "SELECT nope FROM refunds"}),  # an error result
    ("get_work_record", {}),
    ("query_database", {"sql": "SELECT id FROM case_notes"}),  # a truncated result
    ("save_final_answer", {"ids": [3, 1, 2]}),
    ("save_final_answer", {"ids": [4]}),  # an error result
]


async def test_the_log_holds_exactly_what_the_agent_saw(task_dir, make_run, make_meter):
    script = scripted_sequence(
        *(tool_use_response(name, args) for name, args in CALLS), text_response("done")
    )
    client = ScriptedClient(script, run=make_run(), meter=make_meter())
    with TaskEnv.open(task_dir, "task_0000") as env:
        result = await run_tool_loop(
            client,
            system=[text_block("You are a test agent.")],
            messages=[user_message("Go.")],
            tools=TASK_TOOLS,
            handlers=env.handlers(),
            max_turns=10,
            max_tokens=256,
            tags=TAGS,
        )
        sent = [(r.call.name, r.call.input, r.result) for r in result.tool_calls]
        logged = [(entry["tool"], entry["arguments"], entry["result"]) for entry in env.log]

        # Every call except get_work_record, in order, with the very string the loop sent back.
        assert logged == [call for call in sent if call[0] != "get_work_record"]
        assert [entry["seq"] for entry in env.log] == [1, 2, 3, 4, 5, 6]
        assert list(env.log[0]) == ["seq", "tool", "arguments", "result"]
        assert env.log[2]["result"].startswith("error: no such column")
        assert env.log[3]["result"].endswith("…truncated, 1750 more rows)")
        assert env.log[5]["result"] == "error: answer already saved"

        # get_work_record returned the log as it stood at that moment (three entries).
        get_work_record_result = next(
            r.result for r in result.tool_calls if r.call.name == "get_work_record"
        )
        assert get_work_record_result == json.dumps(env.log[:3])
        assert env.log_json() == json.dumps(env.log)

        # log_chars is defined as len(json.dumps(log)).
        assert env.stats().log_chars == len(json.dumps(env.log))
        assert env.outcome().log_chars == len(json.dumps(env.log))


def test_the_log_is_larger_than_the_channel_once_the_schema_is_read(task_dir):
    """Step 3 relies on this: a log that holds the schema dump never fits through 1,000 chars."""
    with TaskEnv.open(task_dir, "task_0000") as env:
        env.call("inspect_database", {})
        assert env.stats().log_chars > 2_000
