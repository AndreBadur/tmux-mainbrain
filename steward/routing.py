"""Deterministic routing — the Intelligent Steward's decision core (Phase 3).

THE DETERMINISTIC/GENERATIVE SPLIT (critical architecture constraint):

  * The SEMANTIC judgments — interpreting a task into ``task_salons``, scoring a
    candidate's ``match_score`` and ``centrality`` — are the Steward AGENT's
    brain. They arrive here as INPUTS. This module does NOT embed an LLM and
    does NOT invent semantic scoring.
  * The DETERMINISTIC choice — apply the recency filter, compute coverage +
    capacity, and pick ONE action from the pipeline.md decision tree — IS this
    code. ``route()`` is a pure function: same inputs -> identical decision.

Decision tree (pipeline.md Phase 3):

    strong match + alive + headroom          -> REUSE
    strong match + dead runtime              -> RESUME (then reuse)
    strong match + context near full          -> EXTRACT_FRESH
    weak match / would pollute / no candidate -> SPAWN

"strong match" = match_score >= strong_match_min AND centrality >= centrality_min
                 AND coverage >= coverage_min (all four criteria participate).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config
from .errors import RoutingError


class Action(str, enum.Enum):
    REUSE = "REUSE"                # strong match, alive, has headroom
    RESUME = "RESUME"             # strong match, but the runtime is dead
    EXTRACT_FRESH = "EXTRACT_FRESH"  # strong match, but context near full
    SPAWN = "SPAWN"              # weak match / pollution risk / no candidate


@dataclass(frozen=True)
class ScoredCandidate:
    """A candidate with its four-criteria evaluation (deterministic, derived)."""

    session_id: Optional[str]
    agent: Optional[str]
    coverage: float          # 0..1 — fraction of task_salons the candidate covers
    match_score: float       # 0..1 — supplied by the Steward's semantic judgment
    centrality: float        # 0..1 — supplied by the Steward's semantic judgment
    headroom_pct: Optional[float]  # 0..100, or None when capacity is UNKNOWN
    context_pct: Optional[float]
    alive: bool
    is_strong: bool
    rank_key: tuple = field(compare=False, default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "coverage": round(self.coverage, 4),
            "match_score": round(self.match_score, 4),
            "centrality": round(self.centrality, 4),
            "headroom_pct": (round(self.headroom_pct, 2)
                             if self.headroom_pct is not None else None),
            "context_pct": self.context_pct,
            "alive": self.alive,
            "is_strong": self.is_strong,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """The deterministic routing outcome, transparent about WHY."""

    action: Action
    chosen_session_id: Optional[str]
    reason: str
    scored_candidates: tuple  # tuple[ScoredCandidate, ...], ranked best-first

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "chosen_session_id": self.chosen_session_id,
            "reason": self.reason,
            "scored_candidates": [c.to_dict() for c in self.scored_candidates],
        }


# --------------------------------------------------------------------------- #
# recency filter (deterministic)
# --------------------------------------------------------------------------- #
def _parse_last_seen(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_recency(candidates: list[dict[str, Any]],
                   recency_filter: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop candidates older than the recency window unless overridden.

    ``recency_filter`` = {days: int, override: bool}. ``override=True`` keeps
    stale candidates (the dev explicitly wants them). A candidate with an
    unparseable/absent ``last_seen`` is kept (cannot prove it is stale).
    """
    if not recency_filter:
        return list(candidates)
    if recency_filter.get("override"):
        return list(candidates)
    days = recency_filter.get("days", config.default_recency_days())
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        seen = _parse_last_seen(cand.get("last_seen"))
        if seen is None or seen >= cutoff:
            kept.append(cand)
    return kept


# --------------------------------------------------------------------------- #
# four-criteria scoring (deterministic given the supplied semantic inputs)
# --------------------------------------------------------------------------- #
def _coverage(task_salons: list[str], candidate_salons: Any) -> float:
    """Fraction of task_salons covered by the candidate's salons (0..1)."""
    if not task_salons:
        return 0.0
    cand = set(candidate_salons or [])
    covered = sum(1 for salon in task_salons if salon in cand)
    return covered / len(task_salons)


def _headroom(context_pct: Any) -> Optional[float]:
    """Free headroom percentage (100 - context_pct).

    Returns None when ``context_pct`` is None/unparseable — CAPACITY-UNKNOWN,
    NOT zero (review iter-1 C3). Phase 1 leaves context_pct nullable for v3
    pre-first-turn and unknown agents; collapsing that to 0 would wrongly
    disable REUSE for exactly those knights.
    """
    if context_pct is None:
        return None
    try:
        return max(0.0, 100.0 - float(context_pct))
    except (TypeError, ValueError):
        return None


def _score_candidate(task_salons: list[str], cand: dict[str, Any]) -> ScoredCandidate:
    coverage = _coverage(task_salons, cand.get("salons"))
    match_score = _clamp01(cand.get("match_score", 0.0))
    centrality = _clamp01(cand.get("centrality", 0.0))
    headroom = _headroom(cand.get("context_pct"))
    is_strong = (
        match_score >= config.strong_match_min()
        and centrality >= config.centrality_min()
        and coverage >= config.coverage_min()
    )
    scored = ScoredCandidate(
        session_id=cand.get("session_id"),
        agent=cand.get("agent"),
        coverage=coverage,
        match_score=match_score,
        centrality=centrality,
        headroom_pct=headroom,
        context_pct=cand.get("context_pct"),
        alive=bool(cand.get("alive")),
        is_strong=is_strong,
    )
    # Rank best-first: strong first, then coverage, match, centrality, headroom.
    # Unknown headroom (None) ranks as 0 for ORDERING only (a measured-headroom
    # candidate is preferred when otherwise equal); the decision logic below
    # treats None distinctly (never as a measured 0%).
    rank_headroom = scored.headroom_pct if scored.headroom_pct is not None else 0.0
    object.__setattr__(scored, "rank_key",
                       (1 if is_strong else 0, coverage, match_score,
                        centrality, rank_headroom))
    return scored


def _clamp01(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, num))


# --------------------------------------------------------------------------- #
# the pure decision
# --------------------------------------------------------------------------- #
def route(task_salons: list[str],
          candidates: list[dict[str, Any]],
          capacity_policy: Optional[dict[str, Any]] = None,
          recency_filter: Optional[dict[str, Any]] = None) -> RoutingDecision:
    """Deterministically choose a routing action. Pure: same inputs -> same out.

    Args:
        task_salons: the salons the task touches (Steward's interpretation).
        candidates: knights-index rows enriched with Steward-supplied
            ``match_score`` and ``centrality`` (0..1). Other fields
            (session_id, agent, essence, salons, context_pct, window, alive,
            last_seen) come from scan_knights.
        capacity_policy: optional overrides {near_full_pct, headroom_min_pct}.
        recency_filter: optional {days, override} — applied FIRST.

    Returns a :class:`RoutingDecision` whose ``reason`` names the criteria that
    drove it, so the Steward can be transparent to the dev.
    """
    if not isinstance(task_salons, list):
        raise RoutingError("task_salons must be a list of salon strings")

    policy = capacity_policy or {}
    near_full = policy.get("near_full_pct", config.near_full_pct())
    headroom_min = policy.get("headroom_min_pct", config.headroom_min_pct())

    viable = _apply_recency(candidates or [], recency_filter)
    scored = sorted(
        (_score_candidate(task_salons, c) for c in viable),
        key=lambda s: s.rank_key,
        reverse=True,
    )
    scored_tuple = tuple(scored)

    if not scored:
        return RoutingDecision(
            Action.SPAWN, None,
            "no candidates after recency filter — spawn a fresh young recruit",
            scored_tuple,
        )

    best = scored[0]
    if not best.is_strong:
        return RoutingDecision(
            Action.SPAWN, None,
            (f"best candidate is a weak/tangential match "
             f"(match={best.match_score:.2f}<{config.strong_match_min():.2f} or "
             f"centrality={best.centrality:.2f}<{config.centrality_min():.2f} or "
             f"coverage={best.coverage:.2f}<{config.coverage_min():.2f}); "
             "spawning fresh to avoid polluting a specialist"),
            scored_tuple,
        )

    # Strong match — now branch on capacity + liveness.
    if best.context_pct is not None and best.context_pct >= near_full:
        return RoutingDecision(
            Action.EXTRACT_FRESH, best.session_id,
            (f"strong match but context near full "
             f"({best.context_pct:.0f}%>={near_full:.0f}%): extract a curated "
             "summary into a fresh recruit rather than reload a heavy context"),
            scored_tuple,
        )

    if not best.alive:
        return RoutingDecision(
            Action.RESUME, best.session_id,
            (f"strong match (match={best.match_score:.2f}, "
             f"centrality={best.centrality:.2f}, coverage={best.coverage:.2f}) "
             "but the runtime is dead: resume the durable session, then reuse"),
            scored_tuple,
        )

    # Capacity-UNKNOWN (context_pct is None): do NOT claim a measured 0%.
    # The match is strong and alive — allow REUSE, but say capacity is unknown
    # so the Steward decides with eyes open (review iter-1 C3).
    if best.headroom_pct is None:
        return RoutingDecision(
            Action.REUSE, best.session_id,
            (f"strong match (match={best.match_score:.2f}, "
             f"centrality={best.centrality:.2f}, coverage={best.coverage:.2f}), "
             "alive, but capacity is UNKNOWN (context_pct unavailable — v3 "
             "pre-first-turn or unknown-window agent): reusing; the Steward "
             "should verify headroom before a heavy task"),
            scored_tuple,
        )

    if best.headroom_pct < headroom_min:
        # Alive strong match, but too little headroom to add work safely.
        return RoutingDecision(
            Action.EXTRACT_FRESH, best.session_id,
            (f"strong match, alive, but headroom {best.headroom_pct:.0f}% "
             f"< {headroom_min:.0f}%: extract + fresh to avoid exhausting the "
             "window mid-task"),
            scored_tuple,
        )

    return RoutingDecision(
        Action.REUSE, best.session_id,
        (f"strong match (match={best.match_score:.2f}, "
         f"centrality={best.centrality:.2f}, coverage={best.coverage:.2f}), "
         f"alive, headroom {best.headroom_pct:.0f}%>={headroom_min:.0f}%: "
         "reuse the living knight"),
        scored_tuple,
    )
