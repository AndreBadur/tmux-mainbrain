"""ritual — Ritual A: the Archmaester, strengthening before battle (Phase 4).

A THIN composition over the APPROVED motor + journey + steward layers (their
public APIs are consumed, never modified). The Archmaester is just another
knight, routed by the Steward by essence; this layer only orchestrates
summon → investigate → DELIVER.

Sacred rule: Ritual A DELIVERS, never DEPOSITS. The synthesis is hypothesis
until battle (Ritual B / Phase 5) proves it. Enforced structurally: the
:class:`RitualPort` carries no mine/deposit capability, and a guard rejects any
port that does.

Public API:
    ritual_a(journey_id, subject, refs, port=REAL_RITUAL_PORT,
             archmaester_agent="wrcp", candidates=None, task_salons=None, cwd=None)
        -> ArchmaesterSynthesis
    build_investigation_prompt(subject, refs) -> str
"""

from __future__ import annotations

from .errors import DepositForbiddenError, RitualError
from .prompt import build_investigation_prompt
from .ritual import REAL_RITUAL_PORT, RitualPort, ritual_a
from .synthesis import ArchmaesterSynthesis

__all__ = [
    "ritual_a",
    "build_investigation_prompt",
    "ArchmaesterSynthesis",
    "RitualPort",
    "REAL_RITUAL_PORT",
    "RitualError",
    "DepositForbiddenError",
]

__version__ = "1.0.0"
