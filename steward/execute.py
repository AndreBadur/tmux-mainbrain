"""Execute a routing decision via the motor + journey layers (Phase 3).

``execute_decision`` is the deterministic dispatcher: given a RoutingDecision it
carries out exactly ONE action, registering/updating the knight in meta.json as
POINTERS ONLY (the journey anti-verbosity guard is respected — this module never
writes essence or any document into meta.json).

All motor/journey calls go through an injectable :class:`StewardPort` so unit
tests drive it with fakes (no real tmux/kiro). Production uses
:data:`REAL_STEWARD_PORT`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import motor
import journey
from motor import MotorError

from .errors import RoutingError
from .inject import inject_knowledge
from .routing import Action, RoutingDecision


@dataclass(frozen=True)
class StewardPort:
    """The motor + journey surface the steward executor calls (injectable)."""

    # motor
    spawn: Callable[..., dict[str, Any]]
    resume: Callable[..., dict[str, Any]]
    deliver: Callable[..., None]
    read_session: Callable[..., Any]
    scan_knights: Callable[..., dict[str, Any]]
    # journey
    register_knight: Callable[..., dict[str, Any]]
    spawn_knight: Callable[..., str]
    get_journey: Callable[..., dict[str, Any]]


REAL_STEWARD_PORT = StewardPort(
    spawn=motor.spawn,
    resume=motor.resume,
    deliver=motor.deliver,
    read_session=motor.read_session,
    scan_knights=motor.scan_knights,
    register_knight=journey.register_knight,
    spawn_knight=journey.spawn_knight,
    get_journey=journey.get_journey,
)


def execute_decision(journey_id: str,
                     role: str,
                     decision: RoutingDecision,
                     agent: str,
                     port: StewardPort = REAL_STEWARD_PORT,
                     cwd: Optional[str] = None,
                     extract_summary: Optional[str] = None,
                     inject_sources: Optional[list] = None,
                     inject_dose: str = "lean") -> dict[str, Any]:
    """Carry out ``decision`` and register/update the knight (pointers only).

    Returns {action, tmux_session, session_id, role}.

    * REUSE  — the chosen session is alive; ensure a runtime exists (resume if
               the kiro session lives but has no tmux), register, return it.
    * RESUME — motor.resume(session_id) -> register/update -> return.
    * EXTRACT_FRESH — spawn fresh + inject the Steward-supplied ``extract_summary``
               (a short curated cutting) so the fresh recruit starts ahead.
    * SPAWN  — journey.spawn_knight (fresh young recruit); optional injection of
               ``inject_sources`` (§3.x birth by curated injection).
    """
    action = decision.action

    if action is Action.REUSE:
        return _do_reuse(journey_id, role, decision, agent, port, cwd)
    if action is Action.RESUME:
        return _do_resume(journey_id, role, decision, agent, port, cwd)
    if action is Action.EXTRACT_FRESH:
        return _do_extract_fresh(journey_id, role, agent, port, cwd,
                                 extract_summary)
    if action is Action.SPAWN:
        return _do_spawn(journey_id, role, agent, port, cwd,
                         inject_sources, inject_dose)
    raise RoutingError(f"unknown routing action: {action!r}")


# --------------------------------------------------------------------------- #
# per-action handlers
# --------------------------------------------------------------------------- #
def _do_reuse(journey_id, role, decision, agent, port, cwd) -> dict[str, Any]:
    session_id = decision.chosen_session_id
    if not session_id:
        raise RoutingError("REUSE decision has no chosen_session_id")

    # The kiro session is alive (context durable). If it lacks a live tmux
    # runtime, rebuild one via resume; otherwise it is already attachable.
    knight = port.read_session(session_id)
    tmux_session = _knight_tmux(knight)
    # A reused knight's durable id is the chosen session; it is resolved iff we
    # actually have a non-empty session_id (we do here — it came from the index).
    resolved = True
    if not tmux_session:
        result = _resume(port, session_id, cwd)
        tmux_session = result["tmux_session"]
        # Honor whatever the port's resume reported (don't blindly assume True).
        resolved = bool(result.get("resolved", True)) and \
            bool(result.get("session_id", session_id))

    port.register_knight(journey_id, role=role, agent=agent,
                         session_id=session_id, tmux_session=tmux_session,
                         resolved=resolved)
    return {"action": "REUSE", "tmux_session": tmux_session,
            "session_id": session_id, "resolved": resolved, "role": role}


def _do_resume(journey_id, role, decision, agent, port, cwd) -> dict[str, Any]:
    session_id = decision.chosen_session_id
    if not session_id:
        raise RoutingError("RESUME decision has no chosen_session_id")
    result = _resume(port, session_id, cwd)
    # Surface the motor's contract rather than hardcoding resolved:True.
    resolved = bool(result.get("resolved", True))
    resolved_id = result.get("session_id", session_id)
    port.register_knight(journey_id, role=role, agent=agent,
                         session_id=resolved_id,
                         tmux_session=result["tmux_session"], resolved=resolved)
    return {"action": "RESUME", "tmux_session": result["tmux_session"],
            "session_id": resolved_id, "resolved": resolved, "role": role}


def _do_extract_fresh(journey_id, role, agent, port, cwd,
                      extract_summary) -> dict[str, Any]:
    # Spawn a fresh recruit (via the journey layer, which registers it), then
    # inject the Steward's short curated summary so it starts ahead.
    spawned = _spawn_fresh(port, journey_id, role, agent, cwd)
    if extract_summary:
        inject_knowledge(spawned["tmux_session"],
                         [{"label": "extract-summary", "text": extract_summary}],
                         dose="lean", port=port)
    return {"action": "EXTRACT_FRESH", "tmux_session": spawned["tmux_session"],
            "session_id": spawned["session_id"], "resolved": spawned["resolved"],
            "role": role}


def _do_spawn(journey_id, role, agent, port, cwd,
              inject_sources, inject_dose) -> dict[str, Any]:
    spawned = _spawn_fresh(port, journey_id, role, agent, cwd)
    if inject_sources:
        inject_knowledge(spawned["tmux_session"], inject_sources,
                         dose=inject_dose, port=port)
    return {"action": "SPAWN", "tmux_session": spawned["tmux_session"],
            "session_id": spawned["session_id"], "resolved": spawned["resolved"],
            "role": role}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _resume(port: StewardPort, session_id: str, cwd) -> dict[str, Any]:
    result = port.resume(session_id, cwd=cwd) if cwd is not None \
        else port.resume(session_id)
    if not result.get("tmux_session"):
        raise RoutingError(f"resume returned no tmux_session for {session_id}")
    return result


def _spawn_fresh(port: StewardPort, journey_id, role, agent, cwd) -> dict[str, Any]:
    """Spawn a fresh recruit and surface its durable-id resolution state.

    ``journey.spawn_knight`` registers the knight (honoring the motor's
    ``{session_id, resolved}`` contract) and returns the tmux_session. We read
    the registered knight back from the journey so the executor can propagate
    whether the durable anchor resolved (review iter-1 C5) — instead of
    hardcoding ``session_id: None``.
    """
    if cwd is not None:
        tmux_session = port.spawn_knight(journey_id, role, agent, cwd=cwd)
    else:
        tmux_session = port.spawn_knight(journey_id, role, agent)

    session_id = None
    resolved = False
    try:
        doc = port.get_journey(journey_id)
        for knight in doc.get("knights", []):
            if knight.get("role") == role:
                session_id = knight.get("session_id")
                resolved = bool(knight.get("resolved"))
                break
    except MotorError:
        # If we cannot read the roster back, report unresolved (fail visible):
        # the Steward must scan_knights to establish the durable anchor.
        session_id, resolved = None, False
    return {"tmux_session": tmux_session, "session_id": session_id,
            "resolved": resolved}


def _knight_tmux(knight: Any) -> Optional[str]:
    """A normalized Knight has no tmux field; alive-but-no-runtime -> None.

    The motor's Knight model tracks the durable session, not the ephemeral
    tmux. We therefore always rebuild a runtime for REUSE via resume unless a
    caller-supplied object exposes ``tmux_session``. Kept as a hook for future
    live-runtime discovery; deterministic and side-effect free.
    """
    return getattr(knight, "tmux_session", None) if knight is not None else None
