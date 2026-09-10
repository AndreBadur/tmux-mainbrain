"""Path + configuration resolution for the motor.

Design rule (pipeline.md Phase 1): "Do NOT hardcode absolute user paths where a
config/env is cleaner; default the palace/journeys paths relative to the repo
root (~/tmux-mainbrain) but allow override."

Resolution order for every path is: explicit env var -> repo-root default.
The kiro sessions root is a global kiro concern, so it defaults to
``~/.kiro/sessions`` but is overridable for testing.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- freshness threshold for the fresh-lock rule (POC 06) --------------------
# A session touched within this many seconds is ALIVE; older -> STALE (dead).
_DEFAULT_FRESH_SECONDS = 900  # 15 minutes


def _env_path(var: str, default: Path) -> Path:
    """Return the expanded path from ``var`` if set, else ``default``."""
    value = os.environ.get(var)
    if value:
        return Path(value).expanduser().resolve()
    return default


def repo_root() -> Path:
    """The Realm's stable ground. Override with ``TMUX_MAINBRAIN_ROOT``.

    Defaults to the parent of this package (``~/tmux-mainbrain``) so the motor
    works out-of-the-box from a checkout without any env setup.
    """
    default = Path(__file__).resolve().parent.parent
    return _env_path("TMUX_MAINBRAIN_ROOT", default)


def palace_dir() -> Path:
    """``palace/`` under the repo root. Override with ``TMUX_MAINBRAIN_PALACE``."""
    return _env_path("TMUX_MAINBRAIN_PALACE", repo_root() / "palace")


def archmaester_dir() -> Path:
    """MemPalace lives here (``palace/archmaester/``)."""
    return palace_dir() / "archmaester"


def steward_index_path() -> Path:
    """``palace/steward/knights-index.json`` — the army cache (rewritten whole)."""
    return palace_dir() / "steward" / "knights-index.json"


def sessions_root() -> Path:
    """The kiro sessions root. Override with ``KIRO_SESSIONS_ROOT`` (tests)."""
    default = Path.home() / ".kiro" / "sessions"
    return _env_path("KIRO_SESSIONS_ROOT", default)


def fresh_threshold_seconds() -> int:
    """Fresh-lock threshold. Override with ``TMUX_MAINBRAIN_FRESH_SECONDS``."""
    raw = os.environ.get("TMUX_MAINBRAIN_FRESH_SECONDS")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass  # fall through to default; never crash on a bad env value
    return _DEFAULT_FRESH_SECONDS
