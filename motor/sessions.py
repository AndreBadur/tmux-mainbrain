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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import (
    MotorError,
    SessionCorruptError,
    SessionNotFoundError,
    UnknownSessionFormatError,
)
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
               since: Optional[int] = None,
               spill: Optional[str] = None,
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
        since: incremental GET cursor — a BYTE OFFSET into the transcript
               previously returned as ``cursor``. When given, only rows AFTER
               that offset are parsed, so the caller GETs only the delta since
               its last read (no re-reading the whole transcript). ``last`` then
               slices within that delta. None (default) = read from the start.
        spill: optional path — write the SELECTED turns to this file (one clean
               ``### ROLE`` block per turn) instead of only returning them in
               memory. Used to extract cut turns for ephemeral-agent creation.
        root: sessions root override (tests); defaults to ``sessions_root()``.

    Returns:
        {session_id, count, turns:[{role, text, ts}], cursor} — turns in
        chronological order (oldest of the returned slice first). ``cursor`` is
        the byte offset consumed (pass it back as ``since`` for the next delta).
        When ``spill`` is given, also ``spill_path`` (the file written).

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
    if since is not None and since < 0:
        raise ValueError("since must be a non-negative byte offset")

    knight = read_session(session_id, root=root)
    transcript = knight.transcript_path
    if not transcript:
        # Fresh/pre-first-turn session, or a session with no transcript on disk.
        return {"session_id": knight.session_id, "count": 0, "turns": [],
                "cursor": since or 0}

    rows, cursor = _read_jsonl_rows_from(Path(transcript), since or 0)
    turns = _reconstruct_turns(knight.fmt, rows, knight.session_id)

    if role != "all":
        turns = [t for t in turns if t["role"] == role]

    selected = turns[-last:] if last < len(turns) else turns
    result: dict[str, Any] = {
        "session_id": knight.session_id,
        "count": len(selected),
        "turns": selected,
        "cursor": cursor,
    }
    if spill is not None:
        result["spill_path"] = _spill_turns(Path(spill), knight.session_id,
                                             selected)
    return result


_VERDICT_MARKER = "FINAL VERDICT"
# Bounded defaults for the read_verdict poll loop (never hang forever).
_VERDICT_TIMEOUT = 120.0
_VERDICT_INTERVAL = 2.0


def _reconstruct_turns(fmt: Optional[SessionFormat],
                       rows: list[dict[str, Any]],
                       session_id: str) -> list[dict[str, Any]]:
    """Dispatch to the format-specific turn reconstruction (shared core).

    The single place both ``read_turns`` and ``read_verdict`` go through so the
    clean-turn logic is never duplicated (reuse, not rewrite).
    """
    if fmt == SessionFormat.V3:
        return _turns_from_v3(rows)
    if fmt == SessionFormat.V1_V2:
        return _turns_from_v1v2(rows)
    raise UnknownSessionFormatError(
        f"cannot read turns for session '{session_id}': unknown format {fmt}"
    )


def _extract_verdict(text: str) -> Optional[str]:
    """Return the text from the LAST ``FINAL VERDICT`` marker to end, intact.

    Preserves the block's internal newlines/structure (NOT flattened). Returns
    None when the marker is absent. The marker line itself is included so the
    caller sees the delimiter it agreed on.
    """
    idx = text.rfind(_VERDICT_MARKER)
    if idx == -1:
        return None
    # Rewind to the start of the marker's own line so a leading indent/prefix
    # on that line is not spuriously dropped, but keep it clean.
    line_start = text.rfind("\n", 0, idx) + 1
    return text[line_start:].strip()


def read_verdict(session_id: str,
                 timeout: float = _VERDICT_TIMEOUT,
                 interval: float = _VERDICT_INTERVAL,
                 root: Optional[Path] = None,
                 sleep_fn: Optional[Any] = None,
                 now_fn: Optional[Any] = None) -> dict[str, Any]:
    """Poll a session incrementally until its last assistant turn carries a
    ``FINAL VERDICT`` block; return ONLY that block (intact, with newlines).

    The GET channel for a concluding knight (never ``peek``). Reads the
    transcript by byte-offset tail, tolerating a partial mid-write last line,
    so it observes NEW turns as they append. On success returns the verdict
    block verbatim from the last ``FINAL VERDICT`` marker to end-of-turn.

    On timeout it does NOT guess — it reports whether the knight is still
    producing (``state: in-progress``) or has clearly finished a turn WITHOUT
    the marker (``state: no-marker``), plus the last turn's tail so the caller
    (orchestrator) can decide to nudge:
        ALWAYS INCLUDE "FINAL VERDICT" IN LAST ANSWER SCOPE.

    Args:
        session_id: the knight's session id.
        timeout: max seconds to poll (bounded; never infinite). Default 120.
        interval: seconds between polls. Default 2.
        root: sessions root override (tests).
        sleep_fn / now_fn: injectable clock seams for deterministic tests.

    Returns:
        success  -> {found: true,  session_id, verdict, turns_seen, cursor}
        timeout  -> {found: false, session_id, state, tail, turns_seen, cursor}
    """
    sleeper = sleep_fn or time.sleep
    clock = now_fn or time.monotonic

    knight = read_session(session_id, root=root)
    fmt = knight.fmt
    sid = knight.session_id

    deadline = clock() + max(timeout, 0.0)
    offset = 0
    rows: list[dict[str, Any]] = []
    turns_seen = 0
    last_offset_at_change = clock()

    while True:
        transcript = _current_transcript(session_id, root)
        if transcript is not None:
            new_rows, offset = _read_jsonl_rows_from(transcript, offset)
            if new_rows:
                rows.extend(new_rows)
                last_offset_at_change = clock()
            turns = _reconstruct_turns(fmt, rows, sid)
            assistant_turns = [t for t in turns if t["role"] == "assistant"]
            turns_seen = len(assistant_turns)
            if assistant_turns:
                verdict = _extract_verdict(assistant_turns[-1]["text"])
                if verdict is not None:
                    return {"found": True, "session_id": sid,
                            "verdict": verdict, "turns_seen": turns_seen,
                            "cursor": offset}

        if clock() >= deadline:
            break
        sleeper(interval)

    # Timed out. Distinguish "still working" from "finished, no marker".
    turns = _reconstruct_turns(fmt, rows, sid)
    assistant_turns = [t for t in turns if t["role"] == "assistant"]
    tail = assistant_turns[-1]["text"][-400:] if assistant_turns else ""
    # If the transcript grew within the last interval, it is likely still
    # producing; otherwise it has settled without emitting the marker.
    still_growing = (clock() - last_offset_at_change) < (interval * 2)
    state = "in-progress" if still_growing else "no-marker"
    return {"found": False, "session_id": sid, "state": state,
            "tail": tail, "turns_seen": len(assistant_turns), "cursor": offset}


def search_turns(query: str,
                 role: str = "all",
                 limit: int = 20,
                 case_sensitive: bool = False,
                 root: Optional[Path] = None) -> dict[str, Any]:
    """Grep-style search THROUGH the turns of ALL sessions on disk.

    Powers the Steward's "search context through old sessions": scans every
    session under the sessions root, reconstructs its clean turns, and returns
    the turns whose text contains ``query``. Content-level (not filename) —
    it matches the actual speech, tool noise already excluded.

    Args:
        query: substring to find in turn text.
        role: restrict to 'assistant' | 'user' | 'all' (default all).
        limit: max matches to return (bounded output).
        case_sensitive: default False (case-insensitive).
        root: sessions root override (tests).

    Returns:
        {query, count, matches:[{session_id, agent, role, snippet, ts,
        turn_index}]}. A corrupt/unknown session is skipped, never fatal.
    """
    if role not in _VALID_ROLES:
        raise ValueError(
            f"invalid role '{role}'; expected one of {', '.join(_VALID_ROLES)}"
        )
    if not query:
        raise ValueError("query must be a non-empty string")

    base = root or sessions_root()
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []

    for path in _iter_session_paths(base):
        if len(matches) >= limit:
            break
        try:
            knight = read_session_path(path)
        except (SessionCorruptError, MotorError):
            continue  # skip a bad session; a search must not abort on one file
        transcript = knight.transcript_path
        if not transcript:
            continue
        try:
            rows = _read_jsonl_rows(Path(transcript))
            turns = _reconstruct_turns(knight.fmt, rows, knight.session_id)
        except (SessionCorruptError, UnknownSessionFormatError):
            continue
        for turn_index, turn in enumerate(turns):
            if role != "all" and turn["role"] != role:
                continue
            hay = turn["text"] if case_sensitive else turn["text"].lower()
            pos = hay.find(needle)
            if pos == -1:
                continue
            matches.append({
                "session_id": knight.session_id,
                "agent": knight.agent,
                "role": turn["role"],
                "snippet": _snippet(turn["text"], pos, len(query)),
                "ts": turn.get("ts"),
                "turn_index": turn_index,
            })
            if len(matches) >= limit:
                break

    return {"query": query, "count": len(matches), "matches": matches}


def _snippet(text: str, pos: int, qlen: int, radius: int = 80) -> str:
    """A short, single-line context window around a match position."""
    start = max(0, pos - radius)
    end = min(len(text), pos + qlen + radius)
    fragment = text[start:end].replace("\n", " ").strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def _spill_turns(path: Path, session_id: str,
                 turns: list[dict[str, Any]]) -> str:
    """Write selected turns to a file as clean ``### ROLE`` blocks. Returns path.

    The extraction primitive for ephemeral-agent creation: cut turns from an
    old session into a file that a new agent can be distilled from.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# turns from session {session_id}", ""]
    for turn in turns:
        header = "### USER" if turn["role"] == "user" else (
            "### AGENT" if turn["role"] == "assistant" else
            f"### {turn['role'].upper()}")
        lines.append(header)
        lines.append(turn["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _current_transcript(session_id: str, root: Optional[Path]) -> Optional[Path]:
    """Re-resolve the transcript path each poll (a fresh session may only get
    its ``messages.jsonl`` after the first turn starts)."""
    try:
        knight = read_session(session_id, root=root)
    except (SessionNotFoundError, SessionCorruptError):
        return None
    tp = knight.transcript_path
    if tp and Path(tp).exists():
        return Path(tp)
    return None


def _read_jsonl_rows_from(transcript: Path,
                          from_offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read jsonl rows starting at ``from_offset`` bytes; return (rows, cursor).

    Only COMPLETE lines are parsed and only the offset PAST the last complete
    line is returned as the new cursor — a partial trailing line (live session
    mid-write) is left unconsumed so the next poll re-reads it whole. This is
    the tail mechanic that makes incremental GET / verdict-polling safe.
    """
    try:
        with transcript.open("rb") as handle:
            handle.seek(from_offset)
            data = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SessionCorruptError(
            f"cannot read transcript {transcript}: {exc}"
        ) from exc

    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], from_offset  # no complete line yet
    complete = data[: last_nl + 1]
    new_offset = from_offset + len(complete)

    rows: list[dict[str, Any]] = []
    for raw in complete.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rows.append(json.loads(raw.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # tolerate a malformed line; keep scanning
    return rows, new_offset


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
