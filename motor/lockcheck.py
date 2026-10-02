"""Stale-lock inspection for a kiro session — the shared safety core.

kiro-cli has NO native lock/force flag (verified: only ``-r`` / ``--resume-id``
/ ``--resume-picker``). A ``.lock`` left behind by a DEAD process makes
``resume`` hang forever on "Initializing". This module decides — SAFELY —
whether that lock may be cleared, so :func:`motor.tmux_driver.resume_clean` can
automate what the operator used to do by hand (``rm`` the stale lock).

The single NON-NEGOTIABLE rule: NEVER remove a lock whose owner is provably
ALIVE (that would allow two runtimes on one session). When in doubt (pid
unparseable / lock unreadable) we REFUSE, not guess.

Lock-path resolution reuses the format adapter's ``source_path`` + ``fmt`` (the
same resolution ``read_session`` / liveness use) — nothing is hardcoded:
  * v3     : ``<session_dir>/.lock``        (source_path IS the session dir)
  * v1/v2  : ``<cli/{uuid}.json>`` -> ``cli/{uuid}.lock`` (sibling of the json)

The pid-liveness probe is an injectable seam (``pid_alive_fn``) defaulting to a
real ``/proc/<pid>`` check on Linux, so callers/tests stay deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .errors import SessionNotFoundError
from .format_adapter import read_session
from .knight_model import SessionFormat

# (pid: int) -> True if a process with that pid is alive.
PidAliveFn = Callable[[int], bool]

# Lock decision states (returned as ``state`` from :func:`inspect_lock`).
LOCK_ABSENT = "absent"        # no .lock -> safe to resume, nothing to clean
LOCK_STALE = "stale"          # lock owned by a DEAD pid -> safe to remove
LOCK_LIVE = "held_by_live"    # lock owned by a LIVE pid -> REFUSE
LOCK_UNKNOWN = "unknown_lock"  # pid unparseable / lock unreadable -> REFUSE


def default_pid_alive(pid: int) -> bool:
    """Real Linux liveness probe: a process is alive iff ``/proc/<pid>`` exists.

    Never raises — a bad pid or a missing /proc simply reads as "not alive".
    """
    try:
        return Path(f"/proc/{int(pid)}").exists()
    except (ValueError, TypeError, OSError):
        return False


def resolve_lock_path(session_id: str,
                      root: Optional[Path] = None) -> Path:
    """Resolve the ``.lock`` path for a session WITHOUT hardcoding layouts.

    Uses ``read_session`` (the same resolver liveness uses) to find the session
    on disk, then derives the lock from its ``source_path`` + ``fmt``. Raises
    :class:`SessionNotFoundError` when the session dir/file does not exist, so
    ``resume_clean`` can fail-soft with a clear reason.
    """
    knight = read_session(session_id, root=root)
    if not knight.source_path:
        raise SessionNotFoundError(
            f"session '{session_id}' has no resolvable source_path"
        )
    source = Path(knight.source_path)
    if knight.fmt == SessionFormat.V3:
        # source_path is the session DIR; lock sits inside it.
        return source / ".lock"
    # v1/v2: source_path is cli/{uuid}.json; lock is the .lock sibling.
    return source.with_suffix(".lock")


def _read_lock_pid(lock_path: Path) -> Optional[int]:
    """Parse the ``pid`` out of a JSON lock file. None when unparseable.

    The lock is JSON like ``{"pid": 1927179, "started_at": "..."}``. Any read /
    parse / type failure returns None so the caller treats it as UNKNOWN
    (refuse) rather than guessing.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    pid = doc.get("pid")
    if isinstance(pid, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(pid, int):
        return pid
    if isinstance(pid, str):
        try:
            return int(pid.strip())
        except (ValueError, TypeError):
            return None
    return None


def inspect_lock(session_id: str,
                 root: Optional[Path] = None,
                 pid_alive_fn: Optional[PidAliveFn] = None) -> dict:
    """Decide the lock state for a session. Pure decision; performs NO removal.

    Returns a dict:
      * absent          -> {state: 'absent',        lock_path}
      * dead pid        -> {state: 'stale',         lock_path, pid}
      * alive pid       -> {state: 'held_by_live',  lock_path, pid}
      * unparseable/... -> {state: 'unknown_lock',  lock_path, pid: None}

    Raises :class:`SessionNotFoundError` if the session cannot be resolved (the
    caller decides how to report that).
    """
    pid_alive = pid_alive_fn or default_pid_alive
    lock_path = resolve_lock_path(session_id, root=root)

    if not lock_path.exists():
        return {"state": LOCK_ABSENT, "lock_path": str(lock_path)}

    pid = _read_lock_pid(lock_path)
    if pid is None:
        return {"state": LOCK_UNKNOWN, "lock_path": str(lock_path), "pid": None}

    if pid_alive(pid):
        return {"state": LOCK_LIVE, "lock_path": str(lock_path), "pid": pid}

    return {"state": LOCK_STALE, "lock_path": str(lock_path), "pid": pid}
