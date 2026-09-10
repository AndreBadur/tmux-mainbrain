"""Steward-layer exceptions — extend the motor/journey hierarchy (Phase 3).

Root is ``motor.MotorError`` (shared across the Realm) so one catch covers
everything, while precise subtypes allow targeted handling.
"""

from __future__ import annotations

from motor import MotorError


class StewardError(MotorError):
    """Base class for every error raised by the steward package."""


class RoutingError(StewardError):
    """A routing decision could not be made or executed as requested."""


class InjectionError(StewardError):
    """A knowledge-injection payload could not be assembled or delivered."""


class SourceNotFoundError(InjectionError):
    """An injection source path does not exist (fail loud — never skip silently)."""


class InjectionTooLargeError(InjectionError):
    """The assembled injection payload exceeded the configured byte cap."""


class DeleteRefusedError(StewardError):
    """A destructive delete was refused (session alive and force not set)."""
