#!/usr/bin/env python3
"""Auto-retain hook for Stop event.

Port of: agent_end handler in Openclaw index.js
Adapted for Claude Code hooks (ephemeral process, JSON stdin/stdout).

Flow:
  1. Read hook input from stdin (session_id, transcript_path, cwd)
  2. Read conversation transcript from transcript_path
  3. Append the unseen transcript suffix, or retain a sliding window in chunked mode
  4. Resolve API URL (external, existing local, or auto-start daemon)
  5. Derive bank ID and ensure mission
  6. Format transcript (strip memory tags, filter roles)
  7. POST to Hindsight retain API (async)

Exit codes:
  0 — always (graceful degradation on any error)
"""

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.bank import derive_bank_id, ensure_bank_mission
from lib.client import HindsightClient
from lib.config import debug_log, load_config
from lib.content import (
    prepare_retention_transcript,
    slice_last_turns_by_user_boundary,
)
from lib.daemon import get_api_url
from lib.state import increment_turn_count


def read_transcript(transcript_path: str) -> list:
    """Read a JSONL transcript file and return list of message dicts.

    Claude Code transcript format nests messages:
      {type: "user", message: {role: "user", content: "..."}, uuid: "...", ...}
    Also supports flat format for testing:
      {role: "user", content: "..."}
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    messages = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Claude Code nested format: {type: "user", message: {role, content}}
                    if entry.get("type") in ("user", "assistant"):
                        msg = entry.get("message", {})
                        if isinstance(msg, dict) and msg.get("role"):
                            messages.append(msg)
                    # Flat format (testing / future compatibility)
                    elif "role" in entry and "content" in entry:
                        messages.append(entry)
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return messages


def run_retain(hook_input: dict, force: bool = False) -> None:
    config = load_config()

    if not config.get("autoRetain"):
        debug_log(config, "Auto-retain disabled, exiting")
        return

    debug_log(config, f"Retain hook_input keys: {list(hook_input.keys())} force={force}")

    session_id = hook_input.get("session_id", "unknown")
    transcript_path = hook_input.get("transcript_path", "")

    # Read full transcript
    all_messages = read_transcript(transcript_path)
    if not all_messages:
        debug_log(config, "No messages in transcript, skipping retain")
        return

    debug_log(config, f"Read {len(all_messages)} messages from transcript")

    # Retention mode: full session (default) or chunked (legacy)
    retain_mode = config.get("retainMode", "full-session")
    retain_every_n = max(1, config.get("retainEveryNTurns", 1))
    retain_full_window = True
    messages_to_retain = all_messages
    document_id = session_id
    chunked_mode = retain_mode == "chunked" and retain_every_n > 1

    # Respect retainEveryNTurns in both modes, unless force=True (SessionEnd final retain)
    if retain_every_n > 1 and not force:
        turn_count = increment_turn_count(session_id)
        if turn_count % retain_every_n != 0:
            next_at = ((turn_count // retain_every_n) + 1) * retain_every_n
            debug_log(config, f"Turn {turn_count}/{retain_every_n}, skipping retain (next at turn {next_at})")
            return

    # Full-session mode keeps one document per session; the existing document is
    # inspected below so the new transcript suffix can be appended safely.
    # Chunked mode uses a distinct timestamped document for each sliding window.
    if chunked_mode:
        # Sliding window: N turns + configured overlap
        overlap_turns = config.get("retainOverlapTurns", 0)
        window_turns = retain_every_n + overlap_turns
        messages_to_retain = slice_last_turns_by_user_boundary(all_messages, window_turns)
        retain_full_window = True
        document_id = f"{session_id}-{int(time.time() * 1000)}"
        debug_log(
            config,
            f"Chunked retain firing (window: {window_turns} turns, {len(messages_to_retain)} messages)",
        )
    else:
        debug_log(
            config,
            f"Full session retain: {len(all_messages)} messages into session document",
        )

    # Format transcript
    retain_roles = config.get("retainRoles", ["user", "assistant"])
    include_tool_calls = config.get("retainToolCalls", True)
    transcript, message_count = prepare_retention_transcript(
        messages_to_retain, retain_roles, retain_full_window, include_tool_calls=include_tool_calls
    )

    if not transcript:
        debug_log(config, "Empty transcript after formatting, skipping retain")
        return

    # Match the API's document-text sanitizer so stored text remains a prefix
    # of the next cumulative transcript, even when Claude emitted ANSI escapes.
    transcript = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", "", transcript)

    # Resolve API URL
    def _dbg(*a):
        debug_log(config, *a)

    try:
        api_url = get_api_url(config, debug_fn=_dbg, allow_daemon_start=True)
    except RuntimeError as e:
        print(f"[Hindsight] {e}", file=sys.stderr)
        return

    api_token = config.get("hindsightApiToken")
    try:
        client = HindsightClient(
            api_url,
            api_token,
            request_timeout_override=config.get("requestTimeoutSeconds"),
        )
    except ValueError as e:
        print(f"[Hindsight] Invalid API URL: {e}", file=sys.stderr)
        return

    # Derive bank ID and ensure mission
    bank_id = derive_bank_id(hook_input, config)
    ensure_bank_mission(client, bank_id, config, debug_fn=_dbg)

    content_to_retain = transcript
    update_mode = None
    if not chunked_mode:
        update_mode = "replace"
        try:
            existing = client.get_document(bank_id, document_id, timeout=10)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                existing = None
            else:
                debug_log(config, f"Could not inspect existing document; skipping retain: {e}")
                return

        existing_text = str((existing or {}).get("original_text", ""))
        if existing_text and transcript.startswith(existing_text):
            content_to_retain = transcript[len(existing_text) :]
            # Hindsight append inserts one newline between the stored document
            # and this suffix; remove one existing boundary newline to preserve
            # the transcript's original two-newline message separator.
            if content_to_retain.startswith("\n"):
                content_to_retain = content_to_retain[1:]
            if not content_to_retain:
                debug_log(config, f"Transcript unchanged for session {session_id}, skipping retain")
                return
            update_mode = "append"
            debug_log(
                config,
                f"Appending transcript suffix: {len(content_to_retain)} chars after "
                f"{len(existing_text)} existing chars",
            )
        elif existing_text:
            debug_log(
                config,
                f"Existing document is not a transcript prefix ({len(existing_text)} chars); "
                "replacing with full snapshot",
            )

    # Resolve template variables in tags and metadata.
    # Supported variables: {session_id}, {bank_id}, {timestamp}, {user_id}
    template_vars = {
        "session_id": session_id,
        "bank_id": bank_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user_id": os.environ.get("HINDSIGHT_USER_ID", ""),
    }

    def _resolve_template(value: str) -> str:
        for k, v in template_vars.items():
            value = value.replace(f"{{{k}}}", v)
        return value

    # Tags from config with template resolution.
    # Drop tags whose resolved form ends in an empty namespace part (e.g. "user:"
    # when HINDSIGHT_USER_ID is unset). Tags without ':' are preserved as-is.
    raw_tags = config.get("retainTags", [])
    if raw_tags:
        tags = []
        for original in raw_tags:
            resolved = _resolve_template(original)
            if ":" in resolved and resolved.split(":", 1)[1] == "":
                debug_log(config, f"Dropping tag '{original}' -> '{resolved}' (empty content after ':')")
                continue
            tags.append(resolved)
        if not tags:
            tags = None
    else:
        tags = None

    # Metadata: merge built-in defaults with user-configured extras
    metadata = {
        "retained_at": template_vars["timestamp"],
        "message_count": str(message_count),
        "session_id": session_id,
    }
    for k, v in config.get("retainMetadata", {}).items():
        metadata[k] = _resolve_template(str(v))

    debug_log(
        config,
        f"Retaining to bank '{bank_id}', doc '{document_id}', {message_count} messages, "
        f"{len(content_to_retain)} chars",
    )
    if tags:
        debug_log(config, f"Tags: {tags}")

    # POST to Hindsight retain API
    try:
        response = client.retain(
            bank_id=bank_id,
            content=content_to_retain,
            document_id=document_id,
            context=config.get("retainContext", "claude-code"),
            metadata=metadata,
            tags=tags,
            update_mode=update_mode,
            timeout=15,
        )
        debug_log(config, f"Retain response: {json.dumps(response)[:200]}")
    except Exception as e:
        print(f"[Hindsight] Retain failed: {e}", file=sys.stderr)


def main():
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        print("[Hindsight] Failed to read hook input", file=sys.stderr)
        return
    run_retain(hook_input, force=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[Hindsight] Unexpected error in retain: {e}", file=sys.stderr)
        try:
            from lib.config import load_config

            sys.exit(2 if load_config().get("debug") else 0)
        except Exception:
            sys.exit(0)
