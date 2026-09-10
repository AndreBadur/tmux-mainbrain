"""Value objects for Ritual B curation (Phase 5).

All frozen — they are faithful snapshots returned to the caller, never mutable
state and never persisted into meta.json (anti-verbosity).

``SaberDrawer`` enforces the MANDATORY provenance flag at construction: a drawer
without an explicit boolean ``validated`` is refused (:class:`ProvenanceError`).
This is the not-omniscient principle in code — the counterpart to Ritual A's
never-deposit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import ProvenanceError


@dataclass(frozen=True)
class KnightReport:
    """One knight's battle report (transient — returned, never persisted)."""

    role: str
    session_id: Optional[str]
    worked: str
    failed: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "session_id": self.session_id,
                "worked": self.worked, "failed": self.failed}


@dataclass(frozen=True)
class SaberDrawer:
    """A curated drawer destined for palace/archmaester/, carrying provenance.

    ``validated`` MUST be an explicit bool (True=battle-proven, False=hypothesis).
    The Archmaester supplies the classification; this object only enforces that
    the flag is present and boolean.
    """

    wing: str
    room: str
    content: str
    validated: bool
    provenance_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"wing": self.wing, "room": self.room, "content": self.content,
                "validated": self.validated,
                "provenance_note": self.provenance_note}


def drawer_from_dict(raw: dict[str, Any]) -> SaberDrawer:
    """Build + validate a SaberDrawer from a raw dict (fail loud on bad shape).

    Refuses a drawer whose ``validated`` flag is ABSENT or non-boolean — the
    mandatory provenance guard (the two-moments principle enforced in code).
    """
    if not isinstance(raw, dict):
        raise ProvenanceError("a saber drawer must be an object")
    if "validated" not in raw:
        raise ProvenanceError(
            "saber drawer missing mandatory 'validated' provenance flag "
            "(true=battle-proven, false=hypothesis) — refusing to deposit "
            "untagged saber (not-omniscient principle)"
        )
    validated = raw["validated"]
    if not isinstance(validated, bool):
        raise ProvenanceError(
            f"'validated' must be a bool, got {type(validated).__name__}"
        )
    for key in ("wing", "room", "content"):
        if not raw.get(key):
            raise ProvenanceError(f"saber drawer missing required '{key}'")
    return SaberDrawer(
        wing=raw["wing"], room=raw["room"], content=raw["content"],
        validated=validated, provenance_note=raw.get("provenance_note", ""),
    )


@dataclass(frozen=True)
class DepositResult:
    """Outcome of depositing curated drawers (or a dry-run plan)."""

    dry_run: bool
    deposited: tuple = field(default=())   # tuple[dict, ...] wing/room/validated

    def to_dict(self) -> dict[str, Any]:
        return {"dry_run": self.dry_run, "deposited": list(self.deposited)}


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of the King's per-knight lifecycle arbitration."""

    applied: tuple = field(default=())     # tuple[dict, ...] {session_id, fate}

    def to_dict(self) -> dict[str, Any]:
        return {"applied": list(self.applied)}


@dataclass(frozen=True)
class RitualBResult:
    """The full Ritual B summary (reports + deposit + lifecycle)."""

    reports: tuple
    deposit: DepositResult
    lifecycle: LifecycleResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "reports": [r.to_dict() for r in self.reports],
            "deposit": self.deposit.to_dict(),
            "lifecycle": self.lifecycle.to_dict(),
        }
