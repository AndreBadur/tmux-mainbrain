"""steward — the Intelligent Steward routing scaffolding (Phase 3).

The Realm's unique value: routing by ESSENCE, not location. Built on the
APPROVED motor (Phase 1) and journey layer (Phase 2); consumes their public
APIs, never modifies them.

THE DETERMINISTIC/GENERATIVE SPLIT
  * Semantic judgments (task->salons, match_score, centrality) are the Steward
    AGENT's brain — they are INPUTS to this code, not hardcoded heuristics.
  * The routing choice, recency filtering, injection assembly, and action
    execution are DETERMINISTIC — this code. ``route()`` is pure.

Public API:
    route(task_salons, candidates, capacity_policy, recency_filter=None)
        -> RoutingDecision
    execute_decision(journey_id, role, decision, agent, port=REAL_STEWARD_PORT, ...)
    inject_knowledge(tmux_session, sources, dose="lean", port=...)
    keep(session_id) / retire(session_id, index) / delete(session_id, force, confirm, ...)
"""

from __future__ import annotations

from .errors import (
    DeleteRefusedError,
    InjectionError,
    InjectionTooLargeError,
    RoutingError,
    SourceNotFoundError,
    StewardError,
)
from .execute import REAL_STEWARD_PORT, StewardPort, execute_decision
from .inject import assemble_payload, inject_knowledge
from .lifecycle import delete, keep, retire
from .routing import Action, RoutingDecision, ScoredCandidate, route

__all__ = [
    # routing
    "route",
    "RoutingDecision",
    "ScoredCandidate",
    "Action",
    # execution
    "execute_decision",
    "StewardPort",
    "REAL_STEWARD_PORT",
    # injection
    "inject_knowledge",
    "assemble_payload",
    # lifecycle
    "keep",
    "retire",
    "delete",
    # errors
    "StewardError",
    "RoutingError",
    "InjectionError",
    "SourceNotFoundError",
    "InjectionTooLargeError",
    "DeleteRefusedError",
]

__version__ = "1.0.0"
