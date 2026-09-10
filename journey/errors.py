"""Journey-layer exceptions — extend the motor's hierarchy (Phase 2).

Reuses ``motor.MotorError`` as the common root so a caller can catch every
Realm error with one type, while still allowing precise handling of
journey-specific failures. No bare excepts anywhere in the journey package.
"""

from __future__ import annotations

from motor import MotorError


class JourneyError(MotorError):
    """Base class for every error raised by the journey package."""


class JourneyNotFoundError(JourneyError):
    """A journey_id was referenced but its meta.json does not exist."""


class JourneyExistsError(JourneyError):
    """Attempted to create a journey_id that already exists."""


class KnightNotFoundError(JourneyError):
    """A role / tmux_session was referenced but is not in the roster."""


class UnresolvedKnightError(JourneyError):
    """A knight's durable session_id is still unresolved when one is required.

    Raised by recovery/resume paths that need the durable anchor; the caller
    must ``resolve_knight`` (via scan) first.
    """


class AntiVerbosityError(JourneyError):
    """A write to meta.json violated the pointers-and-one-liners-only contract.

    Enforces pipeline.md Part II "What CANNOT enter meta.json": forbidden
    fields, multi-line values, transcripts, decisions, domain knowledge.
    """
