"""The journey layer — the MVP "young recruit" loop (pipeline.md Phase 2).

Wires the Phase-0 personalities to the Phase-1 motor. A journey is one
Wise-King session; its ``meta.json`` holds POINTERS ONLY (Part II §6). This
module never modifies the motor's public API — it only consumes it.

The five MVP actions:

  journey_create(goal, king_session)            -> journey_id
  spawn_knight(journey_id, role, agent)          -> tmux_session   (Mode 1)
  command_knight(journey_id, role_or_tmux, prompt) -> result text  (Mode 2)
  recover_knight(journey_id, role)               -> tmux_session   (recovery)
  register_knight / resolve_knight               -> roster maintenance

Testability seam: all motor calls go through a :class:`MotorPort`. Production
uses :data:`REAL_MOTOR` (the actual motor functions); unit tests inject a fake
so NO real tmux/kiro is touched. Deterministic, null-tolerant, bounded — every
motor call already carries its own timeout; the journey layer adds no unbounded
loops (the one re-watch is a single bounded retry).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import motor

from .errors import (
    KnightNotFoundError,
    UnresolvedKnightError,
)
from .meta import read_meta, validate_knight_entry, write_meta
from .paths import meta_path


# --------------------------------------------------------------------------- #
# the motor seam (injectable for tests; defaults to the real motor)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MotorPort:
    """The subset of the motor the journey layer calls, as injectable fns."""

    spawn: Callable[..., dict[str, Any]]
    resume: Callable[..., dict[str, Any]]
    deliver: Callable[..., None]
    watch: Callable[..., dict[str, Any]]
    peek: Callable[..., str]
    scan_knights: Callable[..., dict[str, Any]]


REAL_MOTOR = MotorPort(
    spawn=motor.spawn,
    resume=motor.resume,
    deliver=motor.deliver,
    watch=motor.watch,
    peek=motor.peek,
    scan_knights=motor.scan_knights,
)

# Bounded defaults for command_knight (the motor calls carry their own timeouts).
_COMMAND_WATCH_TIMEOUT = 600
_REWATCH_TIMEOUT = 120
_PEEK_LINES = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# 1. journey_create
# --------------------------------------------------------------------------- #
def journey_create(goal: str,
                   king_session: Optional[str] = None,
                   journey_id: Optional[str] = None) -> str:
    """Create ``journeys/<journey_id>/meta.json`` with the Part II §6 schema.

    ``goal`` must be a single line (enforced by the anti-verbosity guard on
    write). Returns the journey_id.
    """
    if not goal or not goal.strip():
        raise ValueError("goal must be a non-empty one-line string")
    jid = journey_id or f"journey-{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    doc = {
        "journey_id": jid,
        "goal": goal.strip(),
        "king_session": king_session,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "knights": [],
    }
    path = meta_path(jid)
    if path.exists():
        from .errors import JourneyExistsError
        raise JourneyExistsError(f"journey '{jid}' already exists at {path}")
    write_meta(path, doc)  # validates the whole document
    return jid


# --------------------------------------------------------------------------- #
# 2. register_knight / resolve_knight
# --------------------------------------------------------------------------- #
def register_knight(journey_id: str,
                    role: str,
                    agent: str,
                    session_id: Optional[str],
                    tmux_session: str,
                    resolved: Optional[bool] = None) -> dict[str, Any]:
    """Append a POINTER-only knight entry and bump updated_at.

    Handles the motor's spawn result where ``session_id`` may be None with
    ``resolved: false`` — the tmux_session is stored and the id is marked
    unresolved (to be filled later by :func:`resolve_knight`). If a knight with
    the same ``role`` already exists, it is replaced (roster is keyed by role).
    """
    if not tmux_session:
        raise ValueError("tmux_session is required to register a knight")
    if resolved is None:
        resolved = session_id is not None

    entry = {
        "role": role,
        "agent": agent,
        "session_id": session_id,
        "tmux_session": tmux_session,
        "resolved": bool(resolved),
    }
    validate_knight_entry(entry)  # fail fast before touching disk

    path = meta_path(journey_id)
    doc = read_meta(path)
    doc["knights"] = [k for k in doc["knights"] if k.get("role") != role]
    doc["knights"].append(entry)
    doc["updated_at"] = _now_iso()
    write_meta(path, doc)
    return entry


def resolve_knight(journey_id: str,
                   role: str,
                   port: MotorPort = REAL_MOTOR) -> dict[str, Any]:
    """Fill an unresolved knight's durable session_id via a fresh scan.

    Reads the knights-index the motor's ``scan_knights`` rewrites, then matches
    the knight by its agent among sessions not already claimed by another
    resolved knight in this journey. If exactly one candidate is found it is
    stored and ``resolved`` flips true; otherwise the knight stays unresolved
    (no guessing — deterministic).
    """
    path = meta_path(journey_id)
    doc = read_meta(path)
    knight = _find_by_role(doc, role)

    if knight.get("resolved") and knight.get("session_id"):
        return knight  # already resolved; idempotent

    index = port.scan_knights()
    scanned = index.get("knights", [])

    claimed = {k.get("session_id") for k in doc["knights"]
               if k.get("resolved") and k.get("session_id")}
    candidates = [
        s for s in scanned
        if s.get("agent") == knight.get("agent")
        and s.get("session_id")
        and s.get("session_id") not in claimed
    ]
    if len(candidates) == 1:
        knight["session_id"] = candidates[0]["session_id"]
        knight["resolved"] = True
        doc["updated_at"] = _now_iso()
        write_meta(path, doc)
    return knight


# --------------------------------------------------------------------------- #
# 3. spawn_knight (Mode 1 — selection)
# --------------------------------------------------------------------------- #
def spawn_knight(journey_id: str,
                 role: str,
                 agent: str,
                 port: MotorPort = REAL_MOTOR,
                 cwd: Optional[str] = None) -> str:
    """Steward action: spawn a fresh knight, register it, return its tmux_session.

    Honors the motor's spawn contract: ``session_id`` may be None with
    ``resolved: false`` — stored as unresolved for a later ``resolve_knight``.
    """
    result = port.spawn(agent, cwd=cwd) if cwd is not None else port.spawn(agent)
    tmux_session = result.get("tmux_session")
    if not tmux_session:
        raise KnightNotFoundError(
            f"motor.spawn did not return a tmux_session for agent '{agent}'"
        )
    register_knight(
        journey_id,
        role=role,
        agent=agent,
        session_id=result.get("session_id"),
        tmux_session=tmux_session,
        resolved=bool(result.get("resolved")),
    )
    return tmux_session


# --------------------------------------------------------------------------- #
# 4. command_knight (Mode 2 — direct command) + slow-first-token guard
# --------------------------------------------------------------------------- #
def command_knight(journey_id: str,
                   role_or_tmux: str,
                   prompt: str,
                   port: MotorPort = REAL_MOTOR,
                   watch_timeout: int = _COMMAND_WATCH_TIMEOUT,
                   peek_lines: int = _PEEK_LINES) -> str:
    """King action: deliver a prompt to a known knight, watch, return a peek.

    The peek result is RETURNED to the caller only — never persisted into
    meta.json (anti-verbosity).

    Phase-2 hardening (12-review.md MINOR): the motor's stable-idle watch path
    can false-complete on a slow first token when the busy marker never renders.
    Guard: capture a pre-deliver pane baseline; after watch returns, require the
    pane to DIFFER from that baseline (the knight produced new output). If it is
    unchanged, do ONE bounded re-watch before accepting completion.
    """
    if not prompt:
        raise ValueError("prompt must be non-empty")
    tmux_session = _resolve_tmux(journey_id, role_or_tmux)

    baseline = port.peek(tmux_session, peek_lines)
    port.deliver(tmux_session, prompt)  # motor asserts pane-ready by default
    port.watch(tmux_session, timeout=watch_timeout)

    result = port.peek(tmux_session, peek_lines)
    if result == baseline:
        # No new output yet — likely a slow first token that the stable-idle
        # path accepted prematurely. One bounded re-watch, then re-peek.
        port.watch(tmux_session, timeout=_REWATCH_TIMEOUT)
        result = port.peek(tmux_session, peek_lines)

    return result


# --------------------------------------------------------------------------- #
# 5. recover_knight (recovery — tmux gone, kiro session_id survives)
# --------------------------------------------------------------------------- #
def recover_knight(journey_id: str,
                   role: str,
                   port: MotorPort = REAL_MOTOR,
                   cwd: Optional[str] = None) -> str:
    """Rebuild a dead runtime: motor.resume(session_id), update tmux_session.

    Requires a resolved durable ``session_id`` (the anchor). If the knight is
    still unresolved, raises UnresolvedKnightError — the caller must
    ``resolve_knight`` first (the Steward's job).
    """
    path = meta_path(journey_id)
    doc = read_meta(path)
    knight = _find_by_role(doc, role)

    session_id = knight.get("session_id")
    if not knight.get("resolved") or not session_id:
        raise UnresolvedKnightError(
            f"knight '{role}' has no resolved session_id; resolve_knight first"
        )

    result = port.resume(session_id, cwd=cwd) if cwd is not None \
        else port.resume(session_id)
    new_tmux = result.get("tmux_session")
    if not new_tmux:
        raise KnightNotFoundError(
            f"motor.resume did not return a tmux_session for '{session_id}'"
        )
    knight["tmux_session"] = new_tmux
    doc["updated_at"] = _now_iso()
    write_meta(path, doc)
    return new_tmux


# --------------------------------------------------------------------------- #
# read helpers
# --------------------------------------------------------------------------- #
def get_journey(journey_id: str) -> dict[str, Any]:
    """Return the validated meta.json document for a journey."""
    return read_meta(meta_path(journey_id))


def _find_by_role(doc: dict[str, Any], role: str) -> dict[str, Any]:
    for knight in doc.get("knights", []):
        if knight.get("role") == role:
            return knight
    raise KnightNotFoundError(f"no knight with role '{role}' in journey")


def _resolve_tmux(journey_id: str, role_or_tmux: str) -> str:
    """Resolve a role name OR a literal tmux_session to a tmux_session."""
    doc = read_meta(meta_path(journey_id))
    for knight in doc.get("knights", []):
        if knight.get("role") == role_or_tmux:
            return knight["tmux_session"]
        if knight.get("tmux_session") == role_or_tmux:
            return knight["tmux_session"]
    raise KnightNotFoundError(
        f"'{role_or_tmux}' matches no knight role or tmux_session in journey"
    )
