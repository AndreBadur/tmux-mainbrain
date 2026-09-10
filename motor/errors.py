"""Tight, specific exception hierarchy for the deterministic motor.

Maps to pipeline.md Phase 1 design rule: "Robust error handling with tight,
specific exceptions" — never a bare ``except Exception`` that swallows failures.
Every failure mode the motor can hit has a named, catchable type so callers
(the Steward, the test harness) can react precisely.
"""

from __future__ import annotations


class MotorError(Exception):
    """Base class for every error raised by the motor package."""


class UnknownSessionFormatError(MotorError):
    """A session directory/file matched none of the known layouts (v1/v2/v3).

    Raised by the format adapter to FAIL LOUD (pipeline.md POC 07 rule): the
    motor must never silently misread a 3rd, unrecognized kiro format.
    """


class SessionNotFoundError(MotorError):
    """A session id was requested but no matching file/dir exists on disk."""


class SessionCorruptError(MotorError):
    """A session file exists but its JSON/JSONL could not be parsed.

    Distinct from a *fresh* session (which parses fine but is pre-first-turn);
    this means the bytes on disk are structurally broken.
    """


class TmuxError(MotorError):
    """A tmux subprocess returned non-zero or produced unusable output."""


class TmuxTimeoutError(TmuxError):
    """A tmux operation (spawn ready-wait, watch) exceeded its timeout budget.

    The motor never hangs forever — every poll loop is bounded and raises this.
    """


class DeliveryError(MotorError):
    """A prompt could not be delivered to a tmux session (load/paste failed)."""


class MiningError(MotorError):
    """Preprocessing or the mempalace mine subprocess failed."""
