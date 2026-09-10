"""The meta.json store + the anti-verbosity contract (pipeline.md Part II §6/§7).

meta.json holds POINTERS and ONE-LINERS only. This module enforces that rule
IN CODE, not by convention:

  * the top-level document may contain only the allowed keys
    {journey_id, goal, king_session, status, created_at, updated_at, knights};
  * ``goal`` (and every scalar string field) must be a single line;
  * each ``knights[]`` entry may contain ONLY
    {role, agent, session_id, tmux_session, resolved} — nothing else, no
    transcripts, decisions, domain knowledge, logs, or multi-line text.

Any violation raises :class:`~journey.errors.AntiVerbosityError`. Writes are
atomic (temp file + rename) so a reader never sees a half-written state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import AntiVerbosityError, JourneyNotFoundError

# Exact allowed shapes (pipeline.md Part II §6).
_ALLOWED_TOP_KEYS = frozenset(
    {"journey_id", "goal", "king_session", "status", "created_at",
     "updated_at", "knights"}
)
# A knight is a POINTER: role/agent/session_id/tmux_session, plus an internal
# ``resolved`` marker for the motor's spawn {session_id:None, resolved:false}.
_ALLOWED_KNIGHT_KEYS = frozenset(
    {"role", "agent", "session_id", "tmux_session", "resolved"}
)


def _assert_one_line(field: str, value: Any) -> None:
    """Reject a string that spans more than one line (anti-verbosity)."""
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        raise AntiVerbosityError(
            f"field '{field}' must be a single line; multi-line values are "
            "forbidden in meta.json (they belong in pipeline.md / the session)"
        )


def validate_knight_entry(entry: dict[str, Any]) -> None:
    """Raise AntiVerbosityError unless ``entry`` is a pure pointer."""
    if not isinstance(entry, dict):
        raise AntiVerbosityError("a knight entry must be an object")
    extra = set(entry) - _ALLOWED_KNIGHT_KEYS
    if extra:
        raise AntiVerbosityError(
            f"forbidden knight field(s) {sorted(extra)}; a knight is a POINTER: "
            f"{sorted(_ALLOWED_KNIGHT_KEYS)}"
        )
    for key in ("role", "agent", "tmux_session"):
        if not entry.get(key):
            raise AntiVerbosityError(f"knight entry missing required '{key}'")
    for key, value in entry.items():
        _assert_one_line(f"knights[].{key}", value)


def validate_document(doc: dict[str, Any]) -> None:
    """Raise AntiVerbosityError unless ``doc`` obeys the meta.json contract."""
    if not isinstance(doc, dict):
        raise AntiVerbosityError("meta.json must be an object")
    extra = set(doc) - _ALLOWED_TOP_KEYS
    if extra:
        raise AntiVerbosityError(
            f"forbidden top-level field(s) {sorted(extra)}; allowed: "
            f"{sorted(_ALLOWED_TOP_KEYS)}"
        )
    for key in ("journey_id", "goal", "status", "created_at", "updated_at"):
        _assert_one_line(key, doc.get(key))
    # king_session may be None (a journey can predate the king session anchor).
    _assert_one_line("king_session", doc.get("king_session") or "")

    knights = doc.get("knights", [])
    if not isinstance(knights, list):
        raise AntiVerbosityError("'knights' must be a list")
    for entry in knights:
        validate_knight_entry(entry)


def read_meta(path: Path) -> dict[str, Any]:
    """Read + validate a meta.json. Raises JourneyNotFoundError if absent."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except FileNotFoundError as exc:
        raise JourneyNotFoundError(f"no journey meta at {path}") from exc
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise AntiVerbosityError(f"meta.json unreadable at {path}: {exc}") from exc
    validate_document(doc)
    return doc


def write_meta(path: Path, doc: dict[str, Any]) -> None:
    """Validate then atomically write a meta.json (temp file + rename)."""
    validate_document(doc)  # never persist a contract violation
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise AntiVerbosityError(f"failed to write meta {path}: {exc}") from exc
