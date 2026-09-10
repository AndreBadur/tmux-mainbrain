"""Named, documented, env-overridable routing thresholds (Phase 3).

Design rule: the routing cutoffs are EXPLICIT named constants, never magic
numbers buried in logic. Each is overridable via an env var so the dev/Steward
can tune routing without editing code. All scores are 0..1 floats; percentages
are 0..100 to match the knights-index ``context_pct``.
"""

from __future__ import annotations

import os


def _env_float(var: str, default: float) -> float:
    """Read a float env override; ignore malformed values (never crash)."""
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- match-quality cutoffs (0..1) ------------------------------------------
def strong_match_min() -> float:
    """Minimum ``match_score`` for a candidate to count as a STRONG match.

    Below this, reusing the candidate risks pollution -> SPAWN fresh instead.
    Override: ``STEWARD_STRONG_MATCH_MIN`` (default 0.70).
    """
    return _env_float("STEWARD_STRONG_MATCH_MIN", 0.70)


def centrality_min() -> float:
    """Minimum ``centrality`` for the match to be the knight's MAIN subject.

    A high match_score on a merely tangential mention (low centrality) is not a
    strong match — the knight would be polluted by unrelated work.
    Override: ``STEWARD_CENTRALITY_MIN`` (default 0.50).
    """
    return _env_float("STEWARD_CENTRALITY_MIN", 0.50)


def coverage_min() -> float:
    """Minimum salon coverage fraction (0..1) for a strong match.

    coverage = (task_salons covered by the candidate) / (total task_salons).
    Override: ``STEWARD_COVERAGE_MIN`` (default 0.50).
    """
    return _env_float("STEWARD_COVERAGE_MIN", 0.50)


# --- capacity cutoffs (context_pct, 0..100) --------------------------------
def near_full_pct() -> float:
    """At or above this ``context_pct`` a knight is "near full" -> EXTRACT_FRESH.

    Reusing/resuming a near-full session risks running out of window mid-task.
    Override: ``STEWARD_NEAR_FULL_PCT`` (default 80.0).
    """
    return _env_float("STEWARD_NEAR_FULL_PCT", 80.0)


def headroom_min_pct() -> float:
    """Minimum free headroom (100 - context_pct) to REUSE without extracting.

    Must be > (100 - near_full_pct) is NOT required, but headroom below this is
    treated as insufficient to add new work.
    Override: ``STEWARD_HEADROOM_MIN_PCT`` (default 20.0).
    """
    return _env_float("STEWARD_HEADROOM_MIN_PCT", 20.0)


# --- injection + recency ----------------------------------------------------
def max_injection_bytes() -> int:
    """Hard cap on an assembled injection payload (bytes). Fail loud above it.

    Even though the motor's deliver path is "unlimited", an injection that large
    signals a mis-scoped dose (the Steward should have curated cuttings).
    Override: ``STEWARD_MAX_INJECTION_BYTES`` (default 262144 = 256 KiB).
    """
    return _env_int("STEWARD_MAX_INJECTION_BYTES", 262_144)


def default_recency_days() -> int:
    """Default recency window (days) when a filter is requested without one.

    Override: ``STEWARD_DEFAULT_RECENCY_DAYS`` (default 60 ~= 2 months).
    """
    return _env_int("STEWARD_DEFAULT_RECENCY_DAYS", 60)
