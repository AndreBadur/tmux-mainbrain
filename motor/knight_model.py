"""The normalized Knight model — the internal contract every session tool speaks.

Maps to pipeline.md POC 07 (format adapter) + Part II schema 7 (knights-index).
Both v1/v2 (``cli/*.json``) and v3 (``{hash}/sess_*/session.json``) session
layouts are normalized into ONE :class:`Knight` shape so the rest of the motor
(and the Steward) never has to know which on-disk format it came from.

Deterministic only: no LLM, no judgement. Same session bytes -> same Knight.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class Liveness(str, enum.Enum):
    """Vitality of a session, derived by the fresh-lock rule (POC 06)."""

    ALIVE = "alive"        # present AND recently touched (within freshness threshold)
    STALE = "stale"        # present but not touched within the threshold -> treated dead
    NOT_READY = "not-ready"  # fresh/empty session, pre-first-turn (POC 07 null-tolerance)


class SessionFormat(str, enum.Enum):
    """Which on-disk layout a Knight was read from."""

    V1_V2 = "v1_v2"   # ~/.kiro/sessions/cli/{uuid}.json + .jsonl + .lock
    V3 = "v3"         # ~/.kiro/sessions/{hash}/sess_{uuid}/session.json + messages.jsonl


@dataclass(frozen=True)
class Knight:
    """Normalized view of a single kiro session.

    Frozen because the adapter is deterministic: a Knight is a value object, a
    faithful snapshot of the session bytes at read time — not mutable state.
    """

    session_id: str
    agent: Optional[str]            # profession; None for a default mode (vibe/spec)
    purpose: Optional[str]          # the session title
    context_pct: Optional[float]    # None until the first turn completes
    window: Optional[int]           # context window in tokens (agent-dependent)
    alive: bool                     # convenience: liveness == ALIVE
    parent: Optional[str]           # parentSessionId (v3 lineage); None for v1/v2
    # --- context beyond the frozen POC-07 tuple, needed for the index/routing ---
    liveness: Liveness = Liveness.NOT_READY
    fmt: Optional[SessionFormat] = None
    workspace: Optional[str] = None
    last_seen: Optional[str] = None   # ISO-8601 UTC of last modification
    source_path: Optional[str] = None  # dir (v3) or json path (v1/v2) it was read from
    transcript_path: Optional[str] = None  # the .jsonl / messages.jsonl for mining

    def to_index_entry(self) -> dict[str, Any]:
        """Project into the knights-index.json schema (Part II, schema 7).

        ``essence`` and ``salons`` are DERIVED BY THE STEWARD (the brain), not
        the motor, so the motor emits neutral placeholders it can safely own.
        """
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "essence": "",          # Steward fills this (macro-view perception)
            "salons": [],           # Steward fills this (routing tags)
            "purpose": self.purpose,
            "context_pct": self.context_pct,
            "window": self.window,
            "alive": self.alive,
            "last_seen": self.last_seen,
            "parent": self.parent,
            "format": self.fmt.value if self.fmt else None,
        }

    def to_dict(self) -> dict[str, Any]:
        """Full deterministic serialization (used by the CLI JSON output)."""
        raw = asdict(self)
        raw["liveness"] = self.liveness.value
        raw["fmt"] = self.fmt.value if self.fmt else None
        return raw
