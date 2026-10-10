#!/usr/bin/env python3
"""Compatibility adapter for upstream Codex command hooks.

This bridges Every Code's project-hook environment contract to hook scripts
written for upstream Codex `hooks.json` command hooks.

Modes:
- passthrough: write CODE_HOOK_PAYLOAD JSON to child stdin and relay
  stdout/stderr/exit status unchanged.
- additional-context: same as passthrough, but if child stdout is upstream
  hook JSON with `hookSpecificOutput.additionalContext`, emit just that text to
  stdout so `code-rs` injects it into the next model request.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("passthrough", "additional-context"),
        default="passthrough",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("missing command to execute")
    return args


def debug_sink_path() -> Path:
    explicit = os.environ.get("CODE_HOOK_COMPAT_DEBUG_FILE")
    if explicit:
        return Path(explicit)

    base = (
        os.environ.get("CODE_HOME")
        or os.environ.get("CODEX_HOME")
        or os.path.join(os.path.expanduser("~"), ".code")
    )
    return Path(base) / "hook-compat-debug.log"


def emit_debug(message: str) -> None:
    if os.environ.get("CODE_HOOK_COMPAT_DEBUG") != "1":
        return
    path = debug_sink_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(message)
        fh.write("\n")


def child_env() -> dict[str, str]:
    env = os.environ.copy()

    if not env.get("CODE_HOME") and not env.get("CODEX_HOME"):
        code_home = str(Path(__file__).resolve().parents[1])
        env["CODE_HOME"] = code_home
        env["CODEX_HOME"] = code_home

    return env


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    payload = os.environ.get("CODE_HOOK_PAYLOAD", "{}")
    payload_obj = None
    try:
        payload_obj = json.loads(payload)
    except json.JSONDecodeError:
        payload_obj = None

    if isinstance(payload_obj, dict):
        summary = {
            "event": payload_obj.get("event"),
            "session_id": payload_obj.get("session_id"),
            "turn_id": payload_obj.get("turn_id"),
            "transcript_path": payload_obj.get("transcript_path"),
            "model": payload_obj.get("model"),
        }
        emit_debug("[codex-hook-compat] " + json.dumps(summary, sort_keys=True))
    else:
        emit_debug("[codex-hook-compat] unable to parse CODE_HOOK_PAYLOAD as JSON")

    completed = subprocess.run(
        args.command,
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(),
        check=False,
    )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if args.mode == "additional-context":
        trimmed = stdout.strip()
        if trimmed:
            try:
                parsed = json.loads(trimmed)
            except json.JSONDecodeError:
                pass
            else:
                context = parsed.get("hookSpecificOutput", {}).get("additionalContext")
                if isinstance(context, str):
                    stdout = context
                else:
                    stdout = ""

    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))