"""The ArchmaesterSynthesis value object (Phase 4 / Ritual A).

What the King receives from Ritual A. It is HYPOTHESIS until battle (Ritual B)
proves it — hence ``delivered: true, deposited: false`` always. Frozen: a
synthesis is a faithful snapshot the caller holds, not mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ArchmaesterSynthesis:
    """The delivered (never deposited) synthesis of a Ritual A investigation."""

    subject: str
    archmaester_session_id: Optional[str]  # durable anchor; None if unresolved
    synthesis_text: str
    delivered: bool = True
    deposited: bool = False  # Ritual A NEVER deposits — invariant, not a flag to flip

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "archmaester_session_id": self.archmaester_session_id,
            "synthesis_text": self.synthesis_text,
            "delivered": self.delivered,
            "deposited": self.deposited,
        }
