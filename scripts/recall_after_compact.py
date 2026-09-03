#!/usr/bin/env python3
"""Restore Hindsight context on the immediate continuation after compaction."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.bank import derive_bank_id, ensure_bank_mission
from lib.client import HindsightClient
from lib.config import debug_log, load_config
from lib.content import (
    compose_recall_query,
    format_current_time,
    format_memories,
    read_transcript,
    truncate_recall_query,
)
from lib.daemon import get_api_url
from lib.state import write_state


def main():
    config = load_config()
    if not config.get("autoRecall"):
        return

    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    transcript_path = hook_input.get("transcript_path", "")
    messages = read_transcript(transcript_path)
    if not messages:
        debug_log(config, "No transcript messages available after compaction")
        return

    user_messages = [m for m in messages if m.get("role") == "user" and m.get("content")]
    if not user_messages:
        return
    latest_prompt = str(user_messages[-1].get("content", "")).strip()
    if len(latest_prompt) < 5:
        return

    context_turns = max(1, config.get("recallContextTurns", 8))
    roles = config.get("recallRoles", ["user", "assistant"])
    query = compose_recall_query(latest_prompt, messages, context_turns, roles)
    query = truncate_recall_query(
        query, latest_prompt, config.get("recallMaxQueryChars", 2000)
    )

    def debug(*args):
        debug_log(config, *args)

    try:
        api_url = get_api_url(config, debug_fn=debug, allow_daemon_start=False)
        client = HindsightClient(api_url, config.get("hindsightApiToken"))
        bank_id = derive_bank_id(hook_input, config)
        ensure_bank_mission(client, bank_id, config, debug_fn=debug)
        response = client.recall(
            bank_id=bank_id,
            query=query,
            max_tokens=config.get("recallMaxTokens", 1024),
            budget=config.get("recallBudget", "mid"),
            types=config.get("recallTypes"),
            timeout=config.get("recallTimeout", 10),
        )
    except Exception as exc:
        debug(f"Compact recall failed: {exc}")
        return

    results = response.get("results", [])
    if not results:
        return

    context_message = (
        "<hindsight_memories>\n"
        f"{config.get('recallPromptPreamble', '')}\n"
        f"Current time - {format_current_time()}\n\n"
        f"{format_memories(results)}\n"
        "</hindsight_memories>"
    )
    write_state(
        "last_recall.json",
        {
            "context": context_message,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bank_id": bank_id,
            "result_count": len(results),
            "event": "SessionStart:compact",
        },
    )

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context_message,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[Hindsight] Unexpected compact recall error: {exc}", file=sys.stderr)
        sys.exit(0)
