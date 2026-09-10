"""Ritual A — the Archmaester, strengthening before battle (Phase 4).

A THIN orchestration over the APPROVED motor + journey + steward layers. The
Archmaester is JUST ANOTHER KNIGHT: routed by the Steward by essence, spawned
and commanded through the existing machinery. This module composes those calls;
it does NOT re-implement spawn/deliver/watch/route.

DELIVERS-NEVER-DEPOSITS (the sacred Ritual A rule) is enforced structurally: the
:class:`RitualPort` bundles ONLY the capabilities Ritual A needs and carries NO
mine/deposit function, so a deposit is impossible within this flow. A defensive
guard also asserts the port exposes no deposit capability.

The King must be able to work WITHOUT Ritual A — ``ritual_a`` is an optional
call and nothing else in the Realm depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import journey
import steward

from .errors import DepositForbiddenError, RitualError
from .prompt import build_investigation_prompt
from .synthesis import ArchmaesterSynthesis

# Capability names that would constitute a deposit — forbidden in Ritual A.
_DEPOSIT_CAPABILITY_NAMES = ("mine", "deposit", "kg_add", "kg_supersede")

# Default salon for routing an archmaester by essence when the caller supplies
# candidates but no explicit task_salons (the Steward normally supplies these).
_DEFAULT_ARCHMAESTER_ROLE = "archmaester"


@dataclass(frozen=True)
class RitualPort:
    """The (deposit-free) surface Ritual A composes over the lower layers.

    Deliberately excludes any mine/deposit — see module docstring. Injectable so
    unit tests drive fakes with no real tmux/kiro.
    """

    # journey layer
    spawn_knight: Callable[..., str]
    command_knight: Callable[..., str]
    get_journey: Callable[..., dict[str, Any]]
    # steward layer (for the reuse-by-essence path)
    route: Callable[..., Any]
    execute_decision: Callable[..., dict[str, Any]]

    def __post_init__(self) -> None:
        # Defense-in-depth: refuse a port that smuggles in a deposit capability.
        for name in _DEPOSIT_CAPABILITY_NAMES:
            if hasattr(self, name):
                raise DepositForbiddenError(
                    f"RitualPort must not expose a '{name}' capability — "
                    "Ritual A delivers, never deposits"
                )


REAL_RITUAL_PORT = RitualPort(
    spawn_knight=journey.spawn_knight,
    command_knight=journey.command_knight,
    get_journey=journey.get_journey,
    route=steward.route,
    execute_decision=steward.execute_decision,
)


def ritual_a(journey_id: str,
             subject: str,
             refs: Optional[list[dict[str, Any]]] = None,
             port: RitualPort = REAL_RITUAL_PORT,
             archmaester_agent: str = "wrcp",
             candidates: Optional[list[dict[str, Any]]] = None,
             task_salons: Optional[list[str]] = None,
             cwd: Optional[str] = None) -> ArchmaesterSynthesis:
    """Run Ritual A: summon/reuse an archmaester, investigate, DELIVER a synthesis.

    Flow (pipeline.md Phase 4):
      a. If ``candidates`` are supplied (from scan_knights + Steward semantic
         scoring), route via ``steward.route`` to decide reuse-an-archmaester-
         who-studied-X vs spawn-fresh; else spawn a fresh archmaester.
      b. Register/spawn the archmaester as a pointer-only knight in the journey.
      c. Command it with an investigation prompt built from subject + refs.
      d. Watch + peek (handled inside ``command_knight``) -> the synthesis.
      e. Return an ArchmaesterSynthesis (delivered=true, deposited=false).

    The synthesis is RETURNED to the caller only — never persisted into
    meta.json (only pointers live there). It is HYPOTHESIS until Ritual B.

    Raises RitualError on an empty subject/journey_id or a malformed ref.
    """
    if not journey_id or not journey_id.strip():
        raise RitualError("journey_id is required")
    if not subject or not subject.strip():
        raise RitualError("subject is required")

    _assert_no_deposit_capability(port)

    role = _DEFAULT_ARCHMAESTER_ROLE
    tmux_session, session_id = _summon_archmaester(
        port, journey_id, role, archmaester_agent, candidates, task_salons, cwd)

    # Build + deliver the investigation instruction; command_knight delivers,
    # watches for completion, and returns a peek of the synthesis.
    prompt = build_investigation_prompt(subject, refs or [])
    synthesis_text = port.command_knight(journey_id, role, prompt)

    # If the durable id was not captured at summon time, try to read it back
    # from the roster (the journey layer may have resolved it).
    if session_id is None:
        session_id = _lookup_session_id(port, journey_id, role)

    return ArchmaesterSynthesis(
        subject=subject.strip(),
        archmaester_session_id=session_id,
        synthesis_text=synthesis_text,
        delivered=True,
        deposited=False,  # Ritual A invariant — never deposits
    )


# --------------------------------------------------------------------------- #
# summon: reuse-by-essence (via Steward) OR spawn fresh
# --------------------------------------------------------------------------- #
def _summon_archmaester(port, journey_id, role, agent, candidates,
                        task_salons, cwd) -> tuple[Optional[str], Optional[str]]:
    """Return (tmux_session, session_id) for a routed-or-fresh archmaester."""
    if candidates:
        # Reuse-by-essence path: the Steward's pure router picks the action,
        # the executor carries it out (spawn/resume/reuse/extract) and registers
        # the knight. task_salons come from the Steward's interpretation.
        decision = port.route(task_salons or [], candidates)
        result = port.execute_decision(journey_id, role, decision, agent)
        return result.get("tmux_session"), result.get("session_id")

    # No candidates -> spawn a fresh archmaester young recruit.
    tmux_session = _spawn_fresh(port, journey_id, role, agent, cwd)
    session_id = _lookup_session_id(port, journey_id, role)
    return tmux_session, session_id


def _spawn_fresh(port, journey_id, role, agent, cwd) -> str:
    if cwd is not None:
        return port.spawn_knight(journey_id, role, agent, cwd=cwd)
    return port.spawn_knight(journey_id, role, agent)


def _lookup_session_id(port, journey_id, role) -> Optional[str]:
    """Read the archmaester's durable session_id back from the roster (if any).

    A journey/motor read failure (roster not yet written, transient error) means
    the durable anchor is simply not resolved yet -> None; the caller/Steward
    resolves it via scan_knights later. We narrow to the Realm's error root.
    """
    from motor import MotorError

    try:
        doc = port.get_journey(journey_id)
    except MotorError:
        return None
    for knight in doc.get("knights", []):
        if knight.get("role") == role:
            return knight.get("session_id")
    return None


def _assert_no_deposit_capability(port: RitualPort) -> None:
    """Guard: the port must expose no deposit/mine capability (Ritual A rule)."""
    for name in _DEPOSIT_CAPABILITY_NAMES:
        if hasattr(port, name):
            raise DepositForbiddenError(
                f"Ritual A port exposes a forbidden '{name}' capability"
            )
