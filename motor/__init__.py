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
    CapacitateError,
    DeliveryError,
    MiningError,
    MotorError,
    SessionCorruptError,
    SessionNotFoundError,
    TmuxError,
    TmuxTimeoutError,
    UnknownSessionFormatError,
)
from .capacitate import capacitate_from_repo, detect_shape
from .format_adapter import read_session, read_session_path
from .knight_model import Knight, Liveness, SessionFormat
from .lineage import record_lineage
from .mining import mine
from .sessions import context_of, read_turns, read_verdict, scan_knights, search_turns
from .tmux_driver import deliver, peek, resume, resume_clean, spawn, watch

__all__ = [
    # session reading (POCs 03/04/06/07)
    "read_session",
    "read_session_path",
    "scan_knights",
    "context_of",
    "read_turns",
    "read_verdict",
    "search_turns",
    # tmux runtime (POCs 02/08)
    "spawn",
    "resume",
    "resume_clean",
    "deliver",
    "watch",
    "peek",
    # mining (POC 03 + MemPalace)
    "mine",
    # lineage (parent-edge recording for the subagent cascade tree)
    "record_lineage",
    # capacitation (repo URL -> knight capability)
    "capacitate_from_repo",
    "detect_shape",
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
    "CapacitateError",
]

__version__ = "1.0.0"
