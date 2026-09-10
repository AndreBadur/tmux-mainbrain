"""The mining tool — kiro .jsonl -> clean turns -> mempalace (POC 03 + MemPalace).

INVIOLABLE RULE (pipeline.md Phase 1): the kiro ``.jsonl`` is NEVER mined raw.
A raw mine pollutes MemPalace drawers with tool-call noise, reasoning traces,
and framing. This module FIRST preprocesses the transcript into a clean
USER/AGENT conversation, writes it as a single convo file under
``palace/archmaester/<wing>/<room>/``, and only THEN invokes
``mempalace mine --mode convos``.

Handles both transcript shapes (POC 03 + POC 07):
  v1/v2 ``.jsonl``:      {kind: Prompt|AssistantMessage|ToolResults, data:{content:[...]}}
  v3 ``messages.jsonl``: {payload:{type: user|assistant|..., content: "..."}}

Timing is ON-DEMAND (an explicit call), never automatic (design rule).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .errors import MiningError
from .paths import archmaester_dir, palace_dir

_MINE_TIMEOUT = 300  # seconds — bounded; the mine subprocess never hangs forever

# v3 assistant sub-types that are NOT conversational content (drop them).
_V3_ASSISTANT_NOISE = {"Reasoning"}


def _clean_turns_from_v1v2(lines: list[str]) -> list[tuple[str, str]]:
    """Extract (role, text) turns from a v1/v2 ``.jsonl`` transcript."""
    turns: list[tuple[str, str]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("kind")
        data = event.get("data") or {}
        if kind == "Prompt":
            text = _join_text_content(data.get("content"))
            if text:
                turns.append(("user", text))
        elif kind == "AssistantMessage":
            text = _join_text_content(data.get("content"))
            if text:
                turns.append(("assistant", text))
        # ToolResults are deliberately dropped: they are noise for the palace.
    return turns


def _clean_turns_from_v3(lines: list[str]) -> list[tuple[str, str]]:
    """Extract (role, text) turns from a v3 ``messages.jsonl`` transcript."""
    turns: list[tuple[str, str]] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") or {}
        ptype = payload.get("type")
        if ptype == "user":
            text = _as_text(payload.get("content"))
            if text:
                turns.append(("user", text))
        elif ptype == "assistant":
            if payload.get("operationType") in _V3_ASSISTANT_NOISE:
                continue  # drop reasoning traces — not conversational content
            text = _as_text(payload.get("content"))
            if text:
                turns.append(("assistant", text))
        # tool_call/tool_result/turn_*/session_* are noise for mining.
    return turns


def _detect_and_clean(transcript_path: Path) -> list[tuple[str, str]]:
    """Read a transcript and return clean turns, auto-detecting the format."""
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise MiningError(f"transcript not found: {transcript_path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise MiningError(f"cannot read transcript {transcript_path}: {exc}") from exc

    # v3 events carry a 'payload'; v1/v2 events carry a 'kind'. Probe the first
    # well-formed line to decide, then extract with the matching parser.
    is_v3 = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            probe = json.loads(raw)
        except json.JSONDecodeError:
            continue
        is_v3 = "payload" in probe
        break

    turns = _clean_turns_from_v3(lines) if is_v3 else _clean_turns_from_v1v2(lines)
    if not turns:
        raise MiningError(
            f"no conversational turns extracted from {transcript_path} "
            "(empty or non-transcript file) — refusing to mine noise"
        )
    return turns


def _render_convo(turns: list[tuple[str, str]]) -> str:
    """Render clean turns into a simple USER/AGENT convo document."""
    blocks: list[str] = []
    for role, text in turns:
        speaker = "USER" if role == "user" else "AGENT"
        blocks.append(f"### {speaker}\n{text.strip()}")
    return "\n\n".join(blocks) + "\n"


def mine(session_jsonl: str,
         wing: str,
         room: str,
         agent: str = "motor",
         dry_run: bool = False) -> dict[str, object]:
    """Preprocess a kiro transcript to clean turns, then mempalace-mine it.

    Args:
        session_jsonl: path to the kiro ``.jsonl`` / ``messages.jsonl``.
        wing: MemPalace Wing (top-level domain).
        room: Room within the wing (subdomain); becomes a subdirectory.
        agent: recorded on every drawer (default 'motor').
        dry_run: preprocess + write the clean convo, but pass ``--dry-run``.

    Returns {wing, room, turns, convo_path, mined: bool}.

    Raises:
        MiningError: preprocessing produced nothing, or the mine subprocess
                     failed / timed out.
    """
    if not wing or not wing.strip():
        raise ValueError("wing must be a non-empty string")
    if not room or not room.strip():
        raise ValueError("room must be a non-empty string")

    transcript_path = Path(session_jsonl).expanduser()
    turns = _detect_and_clean(transcript_path)

    # Clean convo lands under palace/archmaester/<wing>/<room>/ (POC 03 rule:
    # NEVER point mine at the raw kiro session).
    room_dir = archmaester_dir() / wing / room
    room_dir.mkdir(parents=True, exist_ok=True)
    convo_path = room_dir / f"{transcript_path.stem}.clean.md"
    convo_path.write_text(_render_convo(turns), encoding="utf-8")

    mine_cmd = [
        "mempalace",
        "--palace", str(archmaester_dir()),
        "mine",
        str(room_dir),
        "--mode", "convos",
        "--wing", f"{wing}/{room}",
        "--agent", agent,
    ]
    if dry_run:
        mine_cmd.append("--dry-run")

    try:
        completed = subprocess.run(
            mine_cmd,
            capture_output=True,
            timeout=_MINE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MiningError("mempalace binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MiningError(f"mempalace mine timed out after {_MINE_TIMEOUT}s") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise MiningError(f"mempalace mine failed ({completed.returncode}): {stderr}")

    return {
        "wing": wing,
        "room": room,
        "turns": len(turns),
        "convo_path": str(convo_path),
        "palace": str(archmaester_dir()),
        "mined": not dry_run,
    }


# --------------------------------------------------------------------------- #
# content-extraction helpers (pure)
# --------------------------------------------------------------------------- #
def _join_text_content(content: object) -> str:
    """Flatten a v1/v2 ``content`` list into plain text (text parts only)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("kind") == "text":
            data = item.get("data")
            if isinstance(data, str):
                parts.append(data)
    return "\n".join(parts).strip()


def _as_text(content: object) -> str:
    """Coerce a v3 ``content`` value (usually a plain string) to text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return _join_text_content(content)
    return ""
