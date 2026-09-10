"""THE FORMAT ADAPTER — the spine of the motor (pipeline.md POC 07).

``read_session(id)`` detects the on-disk kiro layout and normalizes it into the
single internal :class:`~motor.knight_model.Knight` model:

    { session_id, agent, purpose, context_pct, window, alive, parent }

Layouts handled (exact field paths verified against live sessions 2026-09-01):

  v1/v2  ~/.kiro/sessions/cli/{uuid}.json (+ .jsonl + .lock)
         agent   = session_state.agent_name
         purpose = title
         ctx%    = session_state.rts_model_state.context_usage_percentage
         window  = session_state.rts_model_state.model_info.context_window_tokens
         alive   = .lock mtime freshness  (fresh-lock rule, POC 06)
         parent  = (none in v1/v2)
         FRESH/EMPTY: session_state == null  -> NOT_READY, never crash (POC 07)

  v3     ~/.kiro/sessions/{workspace-hash}/sess_{uuid}/session.json
             (+ messages.jsonl + publish.cursor)
         agent   = session.json.agentMode  (polymorphic: 'vibe'/'spec' => default)
         purpose = session.json.title
         ctx%    = messages.jsonl payload {type:session_metadata, key:contextUsage}
                     -> value.usagePercentage   (populated only after 1st turn)
         window  = derived from agent (v3 does not persist it in session.json)
         alive   = session.json.lastModifiedAt freshness + status (POC 06/07)
         parent  = session.json.parentSessionId (native lineage)

Any layout matching NEITHER shape -> UnknownSessionFormatError (FAIL LOUD).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .errors import (
    SessionCorruptError,
    SessionNotFoundError,
    UnknownSessionFormatError,
)
from .knight_model import Knight, Liveness, SessionFormat
from .paths import fresh_threshold_seconds, sessions_root

# v3 session.json 'agentMode' values that mean "default mode", NOT a profession.
_DEFAULT_AGENT_MODES = {"vibe", "spec"}

# v3 does not persist the context window; map known agents to their window.
# wrcp/mainbrain = 1M, adeheldb-lab-controller = 200k (POC 07). Unknown -> None.
_AGENT_WINDOW_TOKENS = {
    "wrcp": 1_000_000,
    "mainbrain-workflow-specialist": 1_000_000,
    "adeheldb-lab-controller": 200_000,
}


# --------------------------------------------------------------------------- #
# small pure helpers
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict[str, Any]:
    """Parse a JSON file, translating any failure into a specific error."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SessionNotFoundError(f"session file missing: {path}") from exc
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise SessionCorruptError(f"cannot parse session file {path}: {exc}") from exc


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z') to aware UTC."""
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_fresh(when: Optional[datetime]) -> bool:
    """Fresh-lock rule: touched within the threshold => alive (POC 06)."""
    if when is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - when).total_seconds()
    return 0 <= age_seconds <= fresh_threshold_seconds()


def _normalize_agent(raw_agent: Optional[str]) -> Optional[str]:
    """Collapse default modes to None; otherwise the agent IS the profession."""
    if not raw_agent:
        return None
    if raw_agent in _DEFAULT_AGENT_MODES:
        return None
    return raw_agent


# --------------------------------------------------------------------------- #
# v1 / v2 reader
# --------------------------------------------------------------------------- #
def _read_v1v2(json_path: Path) -> Knight:
    """Normalize a legacy ``cli/{uuid}.json`` session into a Knight."""
    doc = _load_json(json_path)
    session_id = doc.get("session_id") or json_path.stem
    title = doc.get("title")
    state = doc.get("session_state")

    # POC 07 null-tolerance: a fresh/empty session has session_state == null.
    if not isinstance(state, dict):
        return Knight(
            session_id=session_id,
            agent=None,
            purpose=title,
            context_pct=None,
            window=None,
            alive=False,
            parent=None,
            liveness=Liveness.NOT_READY,
            fmt=SessionFormat.V1_V2,
            last_seen=doc.get("updated_at"),
            source_path=str(json_path),
            transcript_path=_sibling(json_path, ".jsonl"),
        )

    agent = _normalize_agent(state.get("agent_name"))
    rts = state.get("rts_model_state") or {}
    context_pct = rts.get("context_usage_percentage")
    window = (rts.get("model_info") or {}).get("context_window_tokens")

    # Liveness fallback contract (review iter-1 C2 MAJOR) — a session with NO
    # .lock (kiro not holding one, or the lock was reaped) must NOT be blindly
    # marked dead off a stale ``updated_at``. We take the FRESHEST of three
    # signals, in priority order but combined so any fresh one wins:
    #   1. .lock mtime          — the canonical live-session marker (POC 06)
    #   2. transcript .jsonl mtime — the session is being written to = active
    #   3. updated_at (JSON)    — last-resort, may lag a live session
    # A session that is genuinely being written to (fresh .jsonl) is ALIVE even
    # when lockless; only when EVERY signal is stale do we report STALE.
    lock_path = _sibling_path(json_path, ".lock")
    jsonl_path = _sibling_path(json_path, ".jsonl")
    lock_when = _lock_mtime(lock_path)
    jsonl_when = _file_mtime(jsonl_path)
    updated_when = _parse_iso(doc.get("updated_at", ""))
    when = _freshest(lock_when, jsonl_when, updated_when)
    liveness = Liveness.ALIVE if _is_fresh(when) else Liveness.STALE

    return Knight(
        session_id=session_id,
        agent=agent,
        purpose=title,
        context_pct=_as_float(context_pct),
        window=_as_int(window),
        alive=liveness == Liveness.ALIVE,
        parent=None,
        liveness=liveness,
        fmt=SessionFormat.V1_V2,
        workspace=doc.get("cwd"),
        last_seen=_iso_or_none(when),
        source_path=str(json_path),
        transcript_path=_sibling(json_path, ".jsonl"),
    )


def _lock_mtime(lock_path: Path) -> Optional[datetime]:
    """Return the lock file's mtime as aware UTC, or None if absent."""
    return _file_mtime(lock_path)


def _file_mtime(path: Path) -> Optional[datetime]:
    """Return any file's mtime as aware UTC, or None if absent/unreadable."""
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


def _freshest(*candidates: Optional[datetime]) -> Optional[datetime]:
    """Return the most recent of the given timestamps, ignoring None."""
    present = [c for c in candidates if c is not None]
    return max(present) if present else None


# --------------------------------------------------------------------------- #
# v3 reader
# --------------------------------------------------------------------------- #
def _v3_liveness(context_pct: Optional[float], status: Optional[str],
                 when: Optional[datetime]) -> Liveness:
    """Decide v3 liveness (review iter-1 C2 MINOR: no dead branch).

    Contract, single real rule set:
      * pre-first-turn (no contextUsage yet)      -> NOT_READY (POC 07)
      * status explicitly 'running'/'working'     -> ALIVE (regardless of mtime)
      * otherwise ALIVE iff lastModifiedAt is fresh, else STALE
    """
    if context_pct is None:
        return Liveness.NOT_READY
    if status in ("running", "working"):
        return Liveness.ALIVE
    return Liveness.ALIVE if _is_fresh(when) else Liveness.STALE


def _read_v3(session_dir: Path) -> Knight:
    """Normalize a v3 ``sess_*/`` directory into a Knight."""
    meta_path = session_dir / "session.json"
    doc = _load_json(meta_path)

    session_id = doc.get("id") or session_dir.name
    title = doc.get("title")
    raw_mode = doc.get("agentMode")
    agent = _normalize_agent(raw_mode)
    parent = doc.get("parentSessionId")
    status = doc.get("status")

    window = _AGENT_WINDOW_TOKENS.get(raw_mode)  # keyed on the raw mode string
    transcript = session_dir / "messages.jsonl"
    context_pct = _read_v3_context_pct(transcript)

    when = _parse_iso(doc.get("lastModifiedAt", ""))
    liveness = _v3_liveness(context_pct, status, when)

    workspace = None
    workspace_paths = doc.get("workspacePaths") or doc.get("rootPaths")
    if isinstance(workspace_paths, list) and workspace_paths:
        workspace = str(workspace_paths[0])

    return Knight(
        session_id=session_id,
        agent=agent,
        purpose=title,
        context_pct=context_pct,
        window=window,
        alive=liveness == Liveness.ALIVE,
        parent=parent,
        liveness=liveness,
        fmt=SessionFormat.V3,
        workspace=workspace,
        last_seen=_iso_or_none(when),
        source_path=str(session_dir),
        transcript_path=str(transcript) if transcript.exists() else None,
    )


def _read_v3_context_pct(transcript: Path) -> Optional[float]:
    """Scan messages.jsonl for the LAST contextUsage metadata event.

    ctx% updates per-turn (POC 07); we want the most recent value, so we keep
    the last match rather than the first. Missing/pre-first-turn -> None.
    """
    if not transcript.exists():
        return None
    latest: Optional[float] = None
    try:
        with transcript.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or "contextUsage" not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a partial trailing line; keep scanning
                payload = event.get("payload") or {}
                if (
                    payload.get("type") == "session_metadata"
                    and payload.get("key") == "contextUsage"
                ):
                    value = payload.get("value") or {}
                    pct = value.get("usagePercentage")
                    parsed = _as_float(pct)
                    if parsed is not None:
                        latest = parsed
    except (OSError, UnicodeDecodeError) as exc:
        raise SessionCorruptError(
            f"cannot read v3 transcript {transcript}: {exc}"
        ) from exc
    return latest


# --------------------------------------------------------------------------- #
# detection + public entry point
# --------------------------------------------------------------------------- #
def _resolve_v3_dir(session_id: str, root: Path) -> Optional[Path]:
    """Find a v3 ``sess_*`` dir for ``session_id`` under any workspace hash."""
    target_name = session_id if session_id.startswith("sess_") else f"sess_{session_id}"
    if not root.exists():
        return None
    for workspace_dir in root.iterdir():
        if not workspace_dir.is_dir() or workspace_dir.name == "cli":
            continue
        candidate = workspace_dir / target_name
        if (candidate / "session.json").exists():
            return candidate
    return None


def _resolve_v1v2_path(session_id: str, root: Path) -> Optional[Path]:
    """Find a v1/v2 ``cli/{uuid}.json`` for ``session_id``."""
    candidate = root / "cli" / f"{session_id}.json"
    return candidate if candidate.exists() else None


def read_session(session_id: str, root: Optional[Path] = None) -> Knight:
    """Read + normalize a session by id, auto-detecting v1/v2 vs v3.

    Args:
        session_id: bare uuid (v1/v2) or ``sess_<uuid>`` / uuid (v3).
        root: sessions root override (tests); defaults to ``sessions_root()``.

    Raises:
        SessionNotFoundError: no matching session on disk.
        UnknownSessionFormatError: a path matched but is neither known layout.
        SessionCorruptError: files exist but cannot be parsed.
    """
    if not session_id or not session_id.strip():
        raise SessionNotFoundError("empty session id")
    base = root or sessions_root()

    v1v2_path = _resolve_v1v2_path(session_id, base)
    if v1v2_path is not None:
        return _read_v1v2(v1v2_path)

    v3_dir = _resolve_v3_dir(session_id, base)
    if v3_dir is not None:
        return _read_v3(v3_dir)

    raise SessionNotFoundError(
        f"no v1/v2 or v3 session found for id '{session_id}' under {base}"
    )


def read_session_path(path: Path) -> Knight:
    """Read + normalize a session given a concrete path (dir or json file).

    Used by :func:`scan_knights` which already walked the tree. FAILs LOUD on
    an unrecognized 3rd format (POC 07): never silently misread.
    """
    path = Path(path)
    if path.is_file() and path.suffix == ".json" and path.parent.name == "cli":
        return _read_v1v2(path)
    if path.is_dir() and (path / "session.json").exists() and path.name.startswith("sess_"):
        return _read_v3(path)
    raise UnknownSessionFormatError(
        f"unrecognized session layout at {path} — refusing to guess (POC 07)"
    )


# --------------------------------------------------------------------------- #
# tiny coercion helpers (kept last: pure, no side effects)
# --------------------------------------------------------------------------- #
def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_or_none(when: Optional[datetime]) -> Optional[str]:
    return when.isoformat().replace("+00:00", "Z") if when else None


def _sibling_path(json_path: Path, suffix: str) -> Path:
    return json_path.with_suffix(suffix)


def _sibling(json_path: Path, suffix: str) -> Optional[str]:
    candidate = json_path.with_suffix(suffix)
    return str(candidate) if candidate.exists() else None
