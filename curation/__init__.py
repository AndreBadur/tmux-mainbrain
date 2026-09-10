"""curation — Ritual B: end-of-journey curation of validated spoils (Phase 5).

The COUNTERPART to Ritual A and the FINAL phase of the Realm. Composes the
APPROVED motor + journey + steward + ritual layers (their public APIs are
consumed, never modified): journey collects knight reports, motor.mine deposits
into palace/archmaester/, steward.keep/retire/delete governs lifecycle. The one
new piece is provenance-tagged curation.

Sacred rule — THE TWO MOMENTS, NEVER CONFUSED:
  * Ritual A (Phase 4): investigate -> deliver -> deposited:false (hypothesis).
  * Ritual B (here):    report -> validate -> DEPOSIT (validated saber).
Every deposited drawer MUST carry an explicit ``validated`` bool (true=battle-
proven, false=hypothesis) — enforced at construction; untagged saber is refused.

Public API:
    collect_spoils(journey_id, port=REAL_CURATION_PORT) -> list[KnightReport]
    curate_saber(subject, synthesis, reports, curated_drawers, port=...) -> list[SaberDrawer]
    deposit_saber(drawers, port=..., dry_run=False) -> DepositResult
    arbitrate_lifecycle(journey_id, decisions, port=...) -> LifecycleResult
    ritual_b(journey_id, subject, synthesis, curated_drawers,
             lifecycle_decisions, port=..., dry_run=False) -> RitualBResult
"""

from __future__ import annotations

from .curation import (
    REAL_CURATION_PORT,
    CurationPort,
    arbitrate_lifecycle,
    collect_spoils,
    curate_saber,
    deposit_saber,
    ritual_b,
)
from .errors import CurationError, DepositError, ProvenanceError
from .models import (
    DepositResult,
    KnightReport,
    LifecycleResult,
    RitualBResult,
    SaberDrawer,
    drawer_from_dict,
)

__all__ = [
    # flow
    "collect_spoils",
    "curate_saber",
    "deposit_saber",
    "arbitrate_lifecycle",
    "ritual_b",
    # seam
    "CurationPort",
    "REAL_CURATION_PORT",
    # models
    "KnightReport",
    "SaberDrawer",
    "DepositResult",
    "LifecycleResult",
    "RitualBResult",
    "drawer_from_dict",
    # errors
    "CurationError",
    "ProvenanceError",
    "DepositError",
]

__version__ = "1.0.0"
