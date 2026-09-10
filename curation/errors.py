"""Curation-layer exceptions — extend the Realm hierarchy (Phase 5 / Ritual B).

Root is ``motor.MotorError`` so one catch spans the whole Realm; specific
subtypes pinpoint the provenance and deposit invariants that make Ritual B the
trustworthy counterpart to Ritual A.
"""

from __future__ import annotations

from motor import MotorError


class CurationError(MotorError):
    """Base class for every error raised by the curation package."""


class ProvenanceError(CurationError):
    """A saber drawer is missing its mandatory ``validated`` provenance flag.

    Enforces the not-omniscient principle: NOTHING is deposited without an
    explicit ``validated: true`` (battle-proven) / ``false`` (hypothesis) tag.
    """


class DepositError(CurationError):
    """A deposit into the palace (via motor.mine) failed."""
