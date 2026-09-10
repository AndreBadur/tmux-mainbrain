"""Ritual-layer exceptions — extend the Realm hierarchy (Phase 4).

Root is ``motor.MotorError`` so one catch covers the whole Realm; specific
subtypes allow precise handling of Ritual A failures and the delivers-never-
deposits invariant.
"""

from __future__ import annotations

from motor import MotorError


class RitualError(MotorError):
    """Base class for every error raised by the ritual package."""


class DepositForbiddenError(RitualError):
    """A deposit/mine was attempted during Ritual A.

    Ritual A only DELIVERS (the synthesis is hypothesis until battle). The
    deposit into the palace happens ONLY in Ritual B (Phase 5). This is raised
    if anything tries to mine/deposit within the Ritual A flow.
    """
