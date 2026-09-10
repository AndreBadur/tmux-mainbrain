"""Path resolution for the journey layer — consistent with motor/paths.py.

The journeys root defaults to ``<repo>/journeys`` and is env-overridable via
``TMUX_MAINBRAIN_JOURNEYS``, reusing the motor's ``repo_root()`` so the two
layers share one stable ground and one override convention.
"""

from __future__ import annotations

import os
from pathlib import Path

from motor.paths import repo_root


def journeys_root() -> Path:
    """``journeys/`` under the repo root. Override with ``TMUX_MAINBRAIN_JOURNEYS``."""
    value = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
    if value:
        return Path(value).expanduser().resolve()
    return repo_root() / "journeys"


def journey_dir(journey_id: str) -> Path:
    """The directory for one journey."""
    return journeys_root() / journey_id


def meta_path(journey_id: str) -> Path:
    """The single state file for a journey (pointers only)."""
    return journey_dir(journey_id) / "meta.json"
