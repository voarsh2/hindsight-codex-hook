#!/usr/bin/env python3
"""Opt-in, project-scoped background reflection experiment."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.bank import derive_bank_id, ensure_bank_mission
from lib.client import HindsightClient
from lib.config import debug_log, load_config
from lib.daemon import get_api_url
from lib.state import increment_turn_count


def reflection_dir() -> Path:
    output_dir = Path.home() / ".hindsight" / "codex" / "reflections"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def write_status(record: dict) -> None:
    """Write a stable latest-status file for local hook diagnostics."""
    path = reflection_dir() / "hindsight-reflect.json"
    try:
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def worker_log_path() -> Path:
    return reflection_dir() / "hindsight-reflect-worker.log"


def log_worker(message: str) -> None:
    try:
        with worker_log_path().open("a", encoding="utf-8") as fh:
            fh.write(
                f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}\n"
            )
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return
    if "--background" in sys.argv:
        write_status({
            "status": "launched",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        fd, path = tempfile.mkstemp(prefix="hindsight-reflect-", suffix=".json")
        os.close(fd)
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
        command = [sys.executable, __file__, "--worker", path]
        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        else:
            popen_kwargs["start_new_session"] = True
        try:
            worker = subprocess.Popen(command, **popen_kwargs)
            write_status({
                "status": "worker_started",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "pid": worker.pid,
            })
        except Exception as exc:
            write_status({
                "status": "failed_to_start",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": str(exc),
            })
            Path(path).unlink(missing_ok=True)
        return
    if "--worker" in sys.argv:
        path = Path(sys.argv[-1])
        write_status({
            "status": "worker_running",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "pid": os.getpid(),
        })
        log_worker(f"worker started pid={os.getpid()} payload={path}")
        try:
            run_reflection(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            write_status({
                "status": "failed",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "error": str(exc),
            })
            log_worker(f"worker failed: {exc!r}")
            print(f"[Hindsight] Reflection failed: {exc}", file=sys.stderr)
        finally:
            path.unlink(missing_ok=True)
        return
    run_reflection(payload)


def run_reflection(hook_input):
    config = load_config()
    if not config.get("autoReflect"):
        write_status({
            "status": "disabled",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return
    session_id = hook_input.get("session_id", "unknown")
    every = max(1, int(config.get("reflectEveryNTurns", 10)))
    turn = increment_turn_count(f"reflect-{session_id}")
    if turn % every:
        write_status({
            "status": "skipped",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "turn": turn,
            "every_n_turns": every,
            "next_due_turn": turn + (every - (turn % every)),
        })
        return

    cwd = str(Path(hook_input.get("cwd") or os.getcwd()).resolve())
    project = Path(cwd).name or cwd
    query = (
        f"{config.get('reflectQuery')}\n\n"
        f"Scope: project '{project}', working directory '{cwd}'. "
        "Ignore unrelated concurrent projects in the same memory bank."
    )
    debug = lambda *args: debug_log(config, *args)
    api_url = get_api_url(config, debug_fn=debug, allow_daemon_start=True)
    client = HindsightClient(api_url, config.get("hindsightApiToken"))
    bank_id = derive_bank_id(hook_input, config)
    ensure_bank_mission(client, bank_id, config, debug_fn=debug)
    started = time.monotonic()
    response = client.reflect(
        bank_id=bank_id, query=query,
        budget=config.get("reflectBudget", "low"),
        context=f"Project-scoped reflection for {project} ({cwd}).",
        max_tokens=config.get("reflectMaxTokens", 1024),
        timeout=config.get("reflectTimeout", 90),
    )
    output_dir = reflection_dir()
    record = {
        "status": "completed",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": project, "cwd": cwd, "bank_id": bank_id,
        "duration_seconds": round(time.monotonic() - started, 2),
        "text": response.get("text") or response.get("answer") or "",
    }
    write_status(record)
    with (output_dir / f"{bank_id}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    debug(f"Reflection completed in {record['duration_seconds']}s")


if __name__ == "__main__":
    main()
