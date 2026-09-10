"""motor — the deterministic engine of the tmux-mainbrain Realm (Phase 1).

The Steward's "hands": pure mechanical functions, no AI/LLM/personality.
Same input -> same output. The Steward (brain) decides the *what*; the motor
provides the *how*, mapping the 8 proven POCs (files 01-08) to reusable tools.

Public API (import) and CLI (``python -m motor <tool> ...``) expose the same
9 tools:

    read_session   scan_knights   context_of                (session reading)
    spawn   resume   deliver   watch   peek                 (tmux runtime)
    mine                                                     (knowledge mining)

See each module docstring for the POC it implements.
"""

from __future__ import annotations

from .errors import (
    DeliveryError,
    MiningError,
    MotorError,
    SessionCorruptError,
    SessionNotFoundError,
    TmuxError,
    TmuxTimeoutError,
    UnknownSessionFormatError,
)
from .format_adapter import read_session, read_session_path
from .knight_model import Knight, Liveness, SessionFormat
from .mining import mine
from .sessions import context_of, read_turns, scan_knights
from .tmux_driver import deliver, peek, resume, spawn, watch

__all__ = [
    # session reading (POCs 03/04/06/07)
    "read_session",
    "read_session_path",
    "scan_knights",
    "context_of",
    "read_turns",
    # tmux runtime (POCs 02/08)
    "spawn",
    "resume",
    "deliver",
    "watch",
    "peek",
    # mining (POC 03 + MemPalace)
    "mine",
    # model + errors
    "Knight",
    "Liveness",
    "SessionFormat",
    "MotorError",
    "UnknownSessionFormatError",
    "SessionNotFoundError",
    "SessionCorruptError",
    "TmuxError",
    "TmuxTimeoutError",
    "DeliveryError",
    "MiningError",
]

__version__ = "1.0.0"
