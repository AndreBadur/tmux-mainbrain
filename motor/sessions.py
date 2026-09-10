"""Session-reading tools built on the format adapter.

Maps to pipeline.md POCs 03/04/06 and Part II schema 7.

  scan_knights()  walks the sessions root (v1/v2 ``cli/`` + v3 ``{hash}/sess_*``),
                  reads each via the adapter, and REWRITES knights-index.json
                  WHOLE (the index is a cache — source of truth is the sessions).
  context_of(id)  reads the context usage % straight from the session file,
                  with NO kiro-cli invocation (POC 04).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import MotorError, SessionCorruptError, UnknownSessionFormatError
from .format_adapter import read_session, read_session_path
from .knight_model import Knight, SessionFormat
from .paths import sessions_root, steward_index_path

# Roles a caller may request for read_turns.
_VALID_ROLES = ("assistant", "user", "all")


def _iter_session_paths(root: Path):
    """Yield every candidate session path under ``root``.

    Yields the KNOWN shapes (v1/v2 ``cli/*.json``, v3 ``{hash}/sess_*``) AND, so
    a systematic format move is actually detectable (review iter-1 C5), any
    workspace-hash subdirectory that looks like a session container but matches
    NEITHER known shape. Those unknown candidates reach ``read_session_path``,
    which fails loud — feeding ``scan_knights``'s ``skipped_unknown_format``.
    """
    if not root.exists():
        return
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name == "cli":
            # v1/v2: flat *.json files (skip .jsonl/.lock/.history siblings).
            for json_file in sorted(entry.glob("*.json")):
                yield json_file
            continue
        # A workspace-hash dir. Enumerate its immediate subdirectories.
        subdirs = [d for d in sorted(entry.iterdir()) if d.is_dir()]
        for sub in subdirs:
            if sub.name.startswith("sess_") and (sub / "session.json").exists():
                yield sub  # known v3 session
            elif _looks_like_session_container(sub):
                # Unknown layout that WANTS to be a session -> surface it so the
                # adapter fails loud instead of us silently ignoring a moved fmt.
                yield sub


def _looks_like_session_container(path: Path) -> bool:
    """Heuristic: a dir that plausibly holds a session but isn't a known shape.

    Only flags dirs that carry a ``.json``/``.jsonl`` payload (i.e. clearly
    intended as a session), so ordinary non-session dirs (``snapshots`` etc.)
    are not false-flagged as a format break.
    """
    if path.name in ("snapshots", "locks", "watch"):
        return False
    try:
        for child in path.iterdir():
            if child.suffix in (".json", ".jsonl"):
                # Has a payload but did NOT match the known v3 shape above.
                return not (path.name.startswith("sess_")
                            and (path / "session.json").exists())
    except OSError:
        return False
    return False


def scan_knights(root: Optional[Path] = None,
                 index_path: Optional[Path] = None,
                 unknown_ratio_threshold: float = 0.5) -> dict[str, Any]:
    """Scan all sessions and rewrite the knights-index.json cache whole.

    Resilience vs FAIL-LOUD (review iter-1 C5 MAJOR):
      * a CORRUPT individual session is skipped and counted under
        ``skipped_corrupt`` (one bad file must not abort the whole scan);
      * an UNRECOGNIZED LAYOUT is counted under ``skipped_unknown_format`` AND
        treated as a possible systematic format break: if ZERO known sessions
        were read while unknown layouts exist, OR the unknown ratio exceeds
        ``unknown_ratio_threshold``, the scan RAISES ``UnknownSessionFormatError``
        (non-zero exit) rather than returning a healthy-looking, silently
        truncated index. This preserves design rule 4 at scale.

    Returns the index document that was written (on the non-raising path).
    """
    base = root or sessions_root()
    target = index_path or steward_index_path()

    knights: list[dict[str, Any]] = []
    skipped_corrupt: list[dict[str, str]] = []
    skipped_unknown_format: list[dict[str, str]] = []

    for path in _iter_session_paths(base):
        try:
            knight: Knight = read_session_path(path)
            knights.append(knight.to_index_entry())
        except UnknownSessionFormatError as exc:
            skipped_unknown_format.append({"path": str(path), "reason": str(exc)})
        except (SessionCorruptError, MotorError) as exc:
            # MotorError here excludes UnknownSessionFormatError (caught above).
            skipped_corrupt.append({"path": str(path), "reason": str(exc)})

    known_count = len(knights)
    unknown_count = len(skipped_unknown_format)
    total_layouts = known_count + unknown_count

    # FAIL LOUD on a systematic format move (rule 4 at scale).
    if unknown_count > 0:
        ratio = unknown_count / total_layouts if total_layouts else 1.0
        if known_count == 0 or ratio >= unknown_ratio_threshold:
            raise UnknownSessionFormatError(
                f"systematic unknown-format break under {base}: "
                f"{unknown_count} unknown vs {known_count} known "
                f"(ratio {ratio:.2f} >= {unknown_ratio_threshold}); "
                "refusing to write a silently-truncated index"
            )

    document = {
        "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sessions_root": str(base),
        "count": known_count,
        "skipped_corrupt": skipped_corrupt,
        "skipped_unknown_format": skipped_unknown_format,
        "knights": knights,
    }
    _atomic_write_json(target, document)
    return document


def context_of(session_id: str, root: Optional[Path] = None) -> dict[str, Any]:
    """Return the context usage of a session (POC 04) without calling kiro-cli.

    Result shape: {session_id, agent, context_pct, window, alive, liveness}.
    ``context_pct`` is None for a fresh/pre-first-turn session (not an error).
    """
    knight = read_session(session_id, root=root)
    return {
        "session_id": knight.session_id,
        "agent": knight.agent,
        "context_pct": knight.context_pct,
        "window": knight.window,
        "alive": knight.alive,
        "liveness": knight.liveness.value,
    }


def read_turns(session_id: str,
               last: int = 1,
               role: str = "assistant",
               root: Optional[Path] = None) -> dict[str, Any]:
    """Return the last ``last`` conversational TURNS of a session, text-only.

    The CONTENT companion to ``read_session`` (which returns metadata only).
    Reads the transcript that the format adapter already resolved
    (``Knight.transcript_path``) and reconstructs clean speeches, EXCLUDING all
    tool_call / tool_result / control events (turn_start, turn_end,
    session_metadata, usage_summary, session_event, session_start,
    pending_interaction, interaction_resolved, compaction). No tools in output.

    A TURN's assistant speech = all ``assistant.content`` chunks of that turn,
    concatenated in order (v3 streams one speech across many chunks). ``--last``
    counts TURNS of the filtered role, not jsonl lines.

    Works across formats via the adapter's ``Knight.fmt``:
      * v3     : {id, timestamp, payload:{type, content}}, grouped by
                 turn_start/turn_end; assistant chunks concatenated per turn.
      * v1/v2  : {version, kind, data:{content:[{kind:'text', data}]}}, where
                 kind Prompt=user, AssistantMessage=assistant, ToolResults=tool;
                 each such message is one turn (v1/v2 has no turn markers).

    Args:
        session_id: bare uuid (v1/v2) or ``sess_<uuid>`` / uuid (v3).
        last: number of turns to return (of the filtered role). Default 1.
        role: 'assistant' (default), 'user', or 'all'.
        root: sessions root override (tests); defaults to ``sessions_root()``.

    Returns:
        {session_id, count, turns:[{role, text, ts}]} — turns in chronological
        order (oldest of the returned slice first).

    Raises:
        ValueError: invalid ``role`` or non-positive ``last``.
        SessionNotFoundError / SessionCorruptError / UnknownSessionFormatError.
    """
    if role not in _VALID_ROLES:
        raise ValueError(
            f"invalid role '{role}'; expected one of {', '.join(_VALID_ROLES)}"
        )
    if last <= 0:
        raise ValueError("last must be a positive integer (turns to return)")

    knight = read_session(session_id, root=root)
    transcript = knight.transcript_path
    if not transcript:
        # Fresh/pre-first-turn session, or a session with no transcript on disk.
        return {"session_id": knight.session_id, "count": 0, "turns": []}

    rows = _read_jsonl_rows(Path(transcript))

    if knight.fmt == SessionFormat.V3:
        turns = _turns_from_v3(rows)
    elif knight.fmt == SessionFormat.V1_V2:
        turns = _turns_from_v1v2(rows)
    else:
        raise UnknownSessionFormatError(
            f"cannot read turns for session '{knight.session_id}': "
            f"unknown format {knight.fmt}"
        )

    if role != "all":
        turns = [t for t in turns if t["role"] == role]

    selected = turns[-last:] if last < len(turns) else turns
    return {
        "session_id": knight.session_id,
        "count": len(selected),
        "turns": selected,
    }


def _read_jsonl_rows(transcript: Path) -> list[dict[str, Any]]:
    """Parse a .jsonl transcript into row dicts, tolerating a partial tail.

    A trailing half-written line (the session may be live) is skipped, not
    fatal — mirrors the adapter's tolerance in ``_read_v3_context_pct``.
    """
    rows: list[dict[str, Any]] = []
    try:
        with transcript.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a partial trailing line
    except (OSError, UnicodeDecodeError) as exc:
        raise SessionCorruptError(
            f"cannot read transcript {transcript}: {exc}"
        ) from exc
    return rows


def _turns_from_v3(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct turns from a v3 messages.jsonl.

    Observed v3 structure (verified against live sessions): a ``user`` payload
    sits BEFORE the ``turn_start`` of the turn it triggers, i.e.

        user → turn_start → [assistant chunks / tool_call / tool_result] → turn_end

    So the user prompt lives in the GAP between turns, not inside one. We
    therefore capture any ``user`` seen while no turn is open and attach it to
    the next turn. Within a turn, the assistant speech is the concatenation of
    all ``assistant.content`` chunks (v3 streams one speech across many chunks).
    tool_call / tool_result / control events contribute NO text.

    Each present role becomes one output turn, emitted user-then-assistant so
    the returned list is chronological.
    """
    turns: list[dict[str, Any]] = []
    # Pending user captured in the gap before the next turn_start.
    pending_user: list[str] = []
    pending_user_ts: Optional[str] = None
    # Assistant accumulator for the currently-open turn.
    cur_assistant: list[str] = []
    assistant_ts: Optional[str] = None
    in_turn = False

    def _flush(user_parts: list[str], user_ts: Optional[str]) -> None:
        if user_parts:
            turns.append({"role": "user", "text": "".join(user_parts),
                          "ts": user_ts})
        if cur_assistant:
            turns.append({"role": "assistant", "text": "".join(cur_assistant),
                          "ts": assistant_ts})

    for row in rows:
        payload = row.get("payload") or {}
        ptype = payload.get("type")
        ts = row.get("timestamp")

        if ptype == "user":
            content = payload.get("content")
            if isinstance(content, str) and content:
                if in_turn:
                    # Defensive: a user inside an open turn — flush the turn
                    # first (its user is this one), then keep it as pending is
                    # wrong; treat it as this turn's user by flushing now.
                    _flush([content], ts)
                    cur_assistant.clear()
                    assistant_ts = None
                    in_turn = False
                else:
                    pending_user.append(content)
                    if pending_user_ts is None:
                        pending_user_ts = ts
            continue

        if ptype == "turn_start":
            if in_turn:
                # A turn opened without its predecessor closing: flush what we
                # have (with any pending user) before starting the new one.
                _flush(pending_user, pending_user_ts)
                pending_user.clear()
                pending_user_ts = None
            cur_assistant.clear()
            assistant_ts = None
            in_turn = True
            continue

        if ptype == "turn_end":
            _flush(pending_user, pending_user_ts)
            pending_user.clear()
            pending_user_ts = None
            cur_assistant.clear()
            assistant_ts = None
            in_turn = False
            continue

        if ptype == "assistant":
            content = payload.get("content")
            if isinstance(content, str) and content:
                cur_assistant.append(content)
                if assistant_ts is None:
                    assistant_ts = ts
        # tool_call / tool_result / session_metadata / usage_summary /
        # session_event / session_start / pending_interaction /
        # interaction_resolved / ... -> no text, ignored.

    # A live session may end mid-turn (no closing turn_end), or a trailing user
    # may have arrived with no turn yet — emit both faithfully.
    if in_turn or pending_user:
        _flush(pending_user, pending_user_ts)

    return turns


def _turns_from_v1v2(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct turns from a v1/v2 cli/{uuid}.jsonl.

    v1/v2 has no turn_start/turn_end. Each message line is a turn:
      kind Prompt          -> user
      kind AssistantMessage-> assistant
      kind ToolResults / Compaction / other -> excluded (no text)
    ``data.content`` is a list of blocks; only ``kind == 'text'`` blocks carry
    speech (``block.data`` str). toolUse / toolResult blocks are excluded.
    """
    turns: list[dict[str, Any]] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "Prompt":
            role = "user"
        elif kind == "AssistantMessage":
            role = "assistant"
        else:
            continue  # ToolResults, Compaction, anything else -> no text

        data = row.get("data") or {}
        text = _v1v2_text_from_blocks(data.get("content"))
        if not text:
            continue  # e.g. an assistant message that was pure toolUse
        turns.append({
            "role": role,
            "text": text,
            "ts": data.get("timestamp") or row.get("timestamp"),
        })
    return turns


def _v1v2_text_from_blocks(content: Any) -> str:
    """Concatenate the ``kind == 'text'`` blocks of a v1/v2 content list."""
    if not isinstance(content, list):
        # Some legacy rows may carry a bare string.
        return content if isinstance(content, str) else ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("kind") == "text":
            data = block.get("data")
            if isinstance(data, str) and data:
                parts.append(data)
    return "".join(parts)


def _atomic_write_json(target: Path, document: dict[str, Any]) -> None:
    """Write JSON atomically (temp file + rename) so a reader never sees half.

    Rewriting the index whole must be crash-safe: a partial write would leave
    the cache corrupt. tempfile in the same dir guarantees an atomic rename.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except OSError as exc:
        # Clean up the temp file on any failure; surface a specific error.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise MotorError(f"failed to write index {target}: {exc}") from exc
