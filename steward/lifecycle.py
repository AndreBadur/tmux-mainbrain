"""Session lifecycle governance — keep / retire / delete (Phase 3).

The Steward applies keep/retire/delete WITH the King at journey end (Ritual B
recap). Discipline enforced here:
  * keep    — a no-op marker (the knight stays; nothing destructive).
  * retire  — mark the knight retired in the knights-index (not summoned, not
              deleted). ``essence`` is NEVER hand-edited (it is derived on scan).
  * delete  — DESTRUCTIVE: removes the kiro session on disk. Refused unless the
              session is not alive OR the caller passes ``force=True`` AND an
              explicit ``confirm=True``. The index is rebuilt by the next scan;
              we do not hand-edit essence.

Deterministic, bounded, tight exceptions. Delete uses the format adapter to
locate the on-disk session (never guesses paths).
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import motor
from motor import MotorError, SessionNotFoundError
from motor.paths import sessions_root

from .errors import DeleteRefusedError, StewardError


def keep(session_id: str) -> dict[str, Any]:
    """Mark a knight as kept (no-op governance record). Non-destructive."""
    if not session_id:
        raise StewardError("session_id is required")
    return {"session_id": session_id, "lifecycle": "keep"}


def retire(session_id: str, index: dict[str, Any]) -> dict[str, Any]:
    """Mark a knight retired IN the provided index document (in memory).

    Sets ``retired: true`` on the matching index row; does NOT touch essence.
    Returns the mutated index (the caller persists it; the authoritative index
    is still rebuilt whole on the next scan_knights).
    """
    if not session_id:
        raise StewardError("session_id is required")
    if not isinstance(index, dict) or "knights" not in index:
        raise StewardError("index must be a knights-index document with 'knights'")

    found = False
    for row in index.get("knights", []):
        if row.get("session_id") == session_id:
            row["retired"] = True
            row["retired_at"] = datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z")
            found = True
            break
    if not found:
        raise StewardError(f"session '{session_id}' not found in index")
    return index


def delete(session_id: str,
           force: bool = False,
           confirm: bool = False,
           port: Optional[Any] = None,
           root: Optional[Path] = None) -> dict[str, Any]:
    """DESTRUCTIVE: remove a kiro session from disk. Heavily gated + path-safe.

    Refuses unless BOTH:
      * ``confirm=True`` (explicit acknowledgement of a destructive act), AND
      * the session is not alive, OR ``force=True``.

    SECURITY (review iter-1 C1 BLOCKER): the ``session_id`` is validated to
    reject path separators / ``..`` / empty / ``.`` BEFORE any filesystem
    access, and every removal path is asserted to be contained within the
    sessions root. Removal prefers the format-adapter-resolved ``source_path``
    over reconstructing paths from the raw id.

    Fail-closed (review C1 MINOR): a session that is present-but-unreadable
    (corrupt) is treated as UNKNOWN and requires ``force`` — only a truly-absent
    session (SessionNotFoundError) is safe to delete without force.

    Returns {session_id, deleted, removed_path}.
    """
    _validate_session_id(session_id)
    if not confirm:
        raise DeleteRefusedError(
            f"delete of '{session_id}' refused: destructive op requires "
            "confirm=True (explicit acknowledgement)"
        )

    read = (port.read_session if port else motor.read_session)
    source_path: Optional[str] = None
    alive = False
    try:
        knight = read(session_id)
        alive = bool(getattr(knight, "alive", False))
        source_path = getattr(knight, "source_path", None)
    except SessionNotFoundError:
        # Truly gone — safe to reap without force.
        alive = False
    except MotorError:
        # Present-but-unreadable (corrupt) or unexpected: fail CLOSED.
        if not force:
            raise DeleteRefusedError(
                f"delete of '{session_id}' refused: session is unreadable "
                "(corrupt/unknown state); pass force=True to delete anyway"
            )
        alive = False

    if alive and not force:
        raise DeleteRefusedError(
            f"delete of '{session_id}' refused: session is ALIVE; pass "
            "force=True to delete a live session deliberately"
        )

    base = root or sessions_root()
    removed = _remove_session_files(session_id, base, source_path)
    return {"session_id": session_id, "deleted": True, "removed_path": removed}


def _validate_session_id(session_id: str) -> None:
    """Reject any id that could escape the sessions root (defense layer a)."""
    if not session_id or session_id in (".", ".."):
        raise StewardError(f"invalid session_id: {session_id!r}")
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        raise StewardError(
            f"session_id must not contain path separators or '..': {session_id!r}"
        )


def _assert_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and assert it stays within ``base`` (layer b)."""
    base_resolved = base.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base_resolved):
        raise StewardError(
            f"refusing to delete outside sessions root: {resolved} not under "
            f"{base_resolved}"
        )
    return resolved


def _remove_session_files(session_id: str, base: Path,
                          source_path: Optional[str] = None) -> Optional[str]:
    """Remove the on-disk session, path-scoped to ``base`` (defense-in-depth).

    Preference order (layer c): if the format adapter resolved a ``source_path``,
    delete exactly that resolved session (its parent dir for v3, its file group
    for v1/v2) after asserting containment. Otherwise fall back to a scoped,
    validated reconstruction from the (already-validated) id.
    """
    if not base.exists():
        return None

    # --- layer c: use the adapter-resolved source_path when available ---------
    if source_path:
        src = Path(source_path)
        if src.name == "session.json":
            # v3: source_path is <base>/<hash>/sess_*/session.json -> remove dir.
            sess_dir = _assert_within(base, src.parent)
            return _rmtree_checked(sess_dir)
        if src.suffix == ".json" and src.parent.name == "cli":
            # v1/v2: remove the <id>.{json,jsonl,history,lock} group.
            return _remove_v1v2_group(base, session_id)

    # --- fallback: scoped reconstruction (id already validated) --------------
    target_name = session_id if session_id.startswith("sess_") else f"sess_{session_id}"
    for workspace_dir in base.iterdir():
        if not workspace_dir.is_dir() or workspace_dir.name == "cli":
            continue
        candidate = workspace_dir / target_name
        if candidate.is_dir():
            safe = _assert_within(base, candidate)
            return _rmtree_checked(safe)

    if (base / "cli").is_dir():
        return _remove_v1v2_group(base, session_id)
    return None


def _remove_v1v2_group(base: Path, session_id: str) -> Optional[str]:
    """Remove cli/<id>.{json,jsonl,history,lock}, each asserted within base."""
    cli = base / "cli"
    removed_any = False
    for suffix in (".json", ".jsonl", ".history", ".lock"):
        candidate = cli / f"{session_id}{suffix}"
        safe = _assert_within(base, candidate)  # raises if it escapes
        if safe.exists():
            try:
                safe.unlink()
                removed_any = True
            except OSError:
                pass
    return str(cli / f"{session_id}.*") if removed_any else None


def _rmtree_checked(sess_dir: Path) -> str:
    """rmtree a session dir and surface a StewardError on partial failure."""
    shutil.rmtree(sess_dir, ignore_errors=True)
    if sess_dir.exists():
        raise StewardError(
            f"session dir not fully removed (partial failure): {sess_dir}"
        )
    return str(sess_dir)
