"""CLI dispatcher — the ONE invocation convention for the Steward.

The Steward calls the motor via ``execute_bash`` running:

    python -m motor <tool> [args...]

Every tool prints a single JSON document to stdout (deterministic, parseable)
and exits 0 on success. On failure it prints ``{"error": "...", "type": "..."}``
to stdout and exits non-zero — so the Steward always gets structured feedback,
never a bare traceback to scrape.

Tools & signatures:
    read_session   <session_id>
    read_turns     <session_id> [--last N] [--role assistant|user|all]
    scan_knights   [--root DIR] [--index PATH]
    context_of     <session_id>
    spawn          <agent> [--tmux NAME] [--cwd DIR] [--ready-timeout N]
    resume         <kiro_session_id> [--tmux NAME] [--cwd DIR] [--ready-timeout N]
    deliver        <tmux_session> --prompt TEXT | --prompt-file PATH
    watch          <tmux_session> [--timeout N] [--sentinel S] [--interval F]
    peek           <tmux_session> [--lines N]
    mine           <session_jsonl> --wing W --room R [--agent A] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    context_of,
    deliver,
    mine,
    peek,
    read_session,
    read_turns,
    resume,
    scan_knights,
    spawn,
    watch,
)
from .errors import MotorError


def _emit(payload: Any) -> None:
    """Print a JSON document to stdout (single-line, deterministic)."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motor", description=__doc__)
    sub = parser.add_subparsers(dest="tool", required=True)

    p = sub.add_parser("read_session")
    p.add_argument("session_id")

    p = sub.add_parser("read_turns")
    p.add_argument("session_id")
    p.add_argument("--last", type=int, default=1,
                   help="number of turns of the filtered role to return (default 1)")
    p.add_argument("--role", choices=["assistant", "user", "all"],
                   default="assistant",
                   help="which interlocutor to return (default assistant)")

    p = sub.add_parser("scan_knights")
    p.add_argument("--root")
    p.add_argument("--index")

    p = sub.add_parser("context_of")
    p.add_argument("session_id")

    p = sub.add_parser("spawn")
    p.add_argument("agent")
    p.add_argument("--tmux")
    p.add_argument("--cwd")
    p.add_argument("--ready-timeout", type=int, default=90)

    p = sub.add_parser("resume")
    p.add_argument("kiro_session_id")
    p.add_argument("--tmux")
    p.add_argument("--cwd")
    p.add_argument("--ready-timeout", type=int, default=90)

    p = sub.add_parser("deliver")
    p.add_argument("tmux_session")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")
    p.add_argument("--no-require-ready", action="store_true",
                   help="skip the ready-state assert (caller MUST watch-for-busy)")

    p = sub.add_parser("watch")
    p.add_argument("tmux_session")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--sentinel")
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--settle", type=float, default=3.0)
    p.add_argument("--idle-stable-samples", type=int, default=3)

    p = sub.add_parser("peek")
    p.add_argument("tmux_session")
    p.add_argument("--lines", type=int, default=200)

    p = sub.add_parser("mine")
    p.add_argument("session_jsonl")
    p.add_argument("--wing", required=True)
    p.add_argument("--room", required=True)
    p.add_argument("--agent", default="motor")
    p.add_argument("--dry-run", action="store_true")

    return parser


def _dispatch(args: argparse.Namespace) -> Any:
    """Route parsed args to the matching tool. Guard clauses, no deep nesting."""
    tool = args.tool

    if tool == "read_session":
        return read_session(args.session_id).to_dict()

    if tool == "read_turns":
        return read_turns(args.session_id, last=args.last, role=args.role)

    if tool == "scan_knights":
        root = Path(args.root) if args.root else None
        index = Path(args.index) if args.index else None
        return scan_knights(root=root, index_path=index)

    if tool == "context_of":
        return context_of(args.session_id)

    if tool == "spawn":
        return spawn(args.agent, tmux_session=args.tmux, cwd=args.cwd,
                     ready_timeout=args.ready_timeout)

    if tool == "resume":
        return resume(args.kiro_session_id, tmux_session=args.tmux, cwd=args.cwd,
                      ready_timeout=args.ready_timeout)

    if tool == "deliver":
        prompt = args.prompt
        if prompt is None:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        deliver(args.tmux_session, prompt,
                require_ready=not args.no_require_ready)
        return {"delivered": True, "tmux_session": args.tmux_session,
                "bytes": len(prompt.encode("utf-8"))}

    if tool == "watch":
        return watch(args.tmux_session, timeout=args.timeout,
                     sentinel=args.sentinel, poll_interval=args.interval,
                     settle_seconds=args.settle,
                     idle_stable_samples=args.idle_stable_samples)

    if tool == "peek":
        return {"tmux_session": args.tmux_session,
                "pane": peek(args.tmux_session, lines=args.lines)}

    if tool == "mine":
        return mine(args.session_jsonl, args.wing, args.room,
                    agent=args.agent, dry_run=args.dry_run)

    raise MotorError(f"unknown tool: {tool}")  # unreachable (argparse guards)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code; never raises to the shell."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except MotorError as exc:
        _emit({"error": str(exc), "type": type(exc).__name__})
        return 1
    except (ValueError, OSError) as exc:
        _emit({"error": str(exc), "type": type(exc).__name__})
        return 2
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
