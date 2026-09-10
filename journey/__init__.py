"""journey — the MVP young-recruit layer of the tmux-mainbrain Realm (Phase 2).

Consumes the APPROVED Phase-1 motor (never modifies its API) to run the minimal
end-to-end journey loop: create a journey, spawn a fresh knight, command it
directly, and recover it after a tmux crash. All state lives in
``journeys/<id>/meta.json`` as POINTERS ONLY, enforced by the anti-verbosity
contract in :mod:`journey.meta`.
"""

from __future__ import annotations

from .errors import (
    AntiVerbosityError,
    JourneyError,
    JourneyExistsError,
    JourneyNotFoundError,
    KnightNotFoundError,
    UnresolvedKnightError,
)
from .journey import (
    MotorPort,
    REAL_MOTOR,
    command_knight,
    get_journey,
    journey_create,
    recover_knight,
    register_knight,
    resolve_knight,
    spawn_knight,
)

__all__ = [
    # actions
    "journey_create",
    "spawn_knight",
    "command_knight",
    "recover_knight",
    "register_knight",
    "resolve_knight",
    "get_journey",
    # seam
    "MotorPort",
    "REAL_MOTOR",
    # errors
    "JourneyError",
    "JourneyNotFoundError",
    "JourneyExistsError",
    "KnightNotFoundError",
    "UnresolvedKnightError",
    "AntiVerbosityError",
]

__version__ = "1.0.0"
