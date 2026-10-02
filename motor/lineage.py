"""Automatic parent-lineage recording for motor-spawned knights.

BACKGROUND (verified): a motor-spawned knight records NO parent/lineage on
disk — a v3 ``session.json`` only carries a ``parentSessionId`` for NATIVE
subagents, never for a knight the motor launched via ``kiro-cli``. So the
parent edge is SEMANTIC — "who COMMANDED this knight" (the requester/spawner),
never who FORGED it — and must be written at spawn time into the journey's
``journeys/<id>/meta.json`` ``knights[]``.

This module isolates that side-effect (Lens 1) from the tmux runtime mechanics
in :mod:`motor.tmux_driver`. Its single public entry point
:func:`record_lineage` is FAIL-SOFT by contract (Lens 3): it NEVER raises — a
missing / unparseable journey file, or any filesystem error, is reported as a
``(False, "<reason>")`` result while the live runtime is left untouched. The
spawn that called it always succeeds regardless.

Level derivation (PARENTING RULE):
  * parent == ``tree_root.session_id``      -> child level = 2
  * parent is another knight in ``knights`` -> child level = that knight + 1
  * parent not found in the file            -> level = None + a warning note
    (never invents a level; never fails the spawn)

The write is atomic (tempfile + ``os.replace``) and preserves EVERY other key
and knight: it loads the full dict, mutates ONLY ``knights[]`` (append-or-update
by ``session_id``, so it is idempotent), and dumps the whole document back.
``tree_root``, ``parenting_rule``, ``tui`` and sibling knights are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _journey_meta_path(journey_id: str) -> Path:
    """Resolve ``journeys/<id>/meta.json``.

    Reuses :func:`journey.paths.meta_path` (the sibling package that already
    owns journey path resolution and the ``TMUX_MAINBRAIN_JOURNEYS`` override),
    mirroring how :mod:`motor.capacitate` resolves journey artifact roots. Falls
    back deterministically to ``<repo>/journeys/<id>/meta.json`` if that import
    is unavailable, so lineage resolution never depends on import ordering.
    """
    try:
        from journey.paths import meta_path  # lazy: journey is a sibling pkg
        return meta_path(journey_id)
    except Exception:  # noqa: BLE001 - deterministic fallback, never crash
        from .paths import repo_root
        return repo_root() / "journeys" / journey_id / "meta.json"


def _derive_child_level(document: dict[str, Any],
                        parent_session_id: str) -> tuple[Optional[int], Optional[str]]:
    """Compute the child's level from the parent's position in ``document``.

    Returns ``(level, warning)``. ``level`` is None (with a warning) when the
    parent cannot be located — the caller then records the knight with
    ``parent`` set and ``level: null`` rather than inventing a number.
    """
    tree_root = document.get("tree_root") or {}
    if tree_root.get("session_id") == parent_session_id:
        root_level = tree_root.get("level")
        # tree_root is level 1 by contract; child is one deeper.
        base = root_level if isinstance(root_level, int) else 1
        return base + 1, None

    for knight in document.get("knights") or []:
        if not isinstance(knight, dict):
            continue
        if knight.get("session_id") == parent_session_id:
            parent_level = knight.get("level")
            if isinstance(parent_level, int):
                return parent_level + 1, None
            # Parent exists but has no numeric level (itself unresolved).
            return None, (
                f"parent '{parent_session_id}' found but has non-numeric "
                f"level {parent_level!r}; recorded child level=null"
            )

    return None, (
        f"parent '{parent_session_id}' not found in journey meta "
        "(neither tree_root nor any knight); recorded child level=null"
    )


def _atomic_write_meta(target: Path, document: dict[str, Any]) -> None:
    """Atomically rewrite the whole journey meta.json (tempfile + rename).

    Reuses the motor's established atomic-write pattern (see
    ``sessions._atomic_write_json``) so a reader never observes a half-written
    file. Imported lazily to avoid any load-time coupling between the driver and
    the sessions module.
    """
    from .sessions import _atomic_write_json
    _atomic_write_json(target, document)


def record_lineage(journey_id: str,
                   session_id: str,
                   tmux_session: str,
                   agent: str,
                   parent_session_id: str,
                   role: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Append-or-update the spawned knight's lineage in the journey meta.json.

    FAIL-SOFT: returns ``(recorded, warning)`` and NEVER raises. On success
    ``(True, None)`` (or ``(True, warning)`` when the parent was unresolvable
    but the knight was still recorded with ``parent`` set + ``level: null``). On
    a missing / unreadable / unparseable journey file, or any write failure,
    returns ``(False, "<reason>")`` — the live runtime is unaffected.

    Idempotent: a knight already present (matched by ``session_id``) has its
    pointer fields UPDATED in place; it is never duplicated. Every other key in
    the document (``tree_root``, ``parenting_rule``, ``tui``, sibling knights)
    is preserved verbatim.

    Args:
        journey_id: journey whose ``meta.json`` to update (e.g. ``journey-tui``).
        session_id: the resolved kiro session id of the spawned knight.
        tmux_session: the tmux session name hosting the knight.
        agent: the kiro agent name the knight runs.
        parent_session_id: session id of who COMMANDS the knight (the spawner).
        role: optional explicit role label; defaults to ``agent`` when absent.
    """
    # Guard clauses: the caller only invokes this when both are present, but be
    # defensive so a bad call reports rather than crashes.
    if not journey_id:
        return False, "no journey id given; lineage not recorded"
    if not session_id:
        return False, "spawn did not resolve a session_id; lineage not recorded"
    if not parent_session_id:
        return False, "no parent session_id given; lineage not recorded"

    meta_file = _journey_meta_path(journey_id)

    try:
        raw = meta_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        return False, f"journey meta unreadable ({meta_file}): {exc}"

    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return False, f"journey meta unparseable ({meta_file}): {exc}"

    if not isinstance(document, dict):
        return False, f"journey meta is not a JSON object ({meta_file})"

    level, level_warning = _derive_child_level(document, parent_session_id)
    resolved_role = role or agent

    knights = document.get("knights")
    if not isinstance(knights, list):
        knights = []
        document["knights"] = knights

    note = f"spawned via motor --parent by {parent_session_id}"
    if level is None:
        note = f"{note}; parent not resolvable in meta, level=null"

    # Idempotent append-or-update by session_id: mutate pointer fields only.
    existing: Optional[dict[str, Any]] = None
    for knight in knights:
        if isinstance(knight, dict) and knight.get("session_id") == session_id:
            existing = knight
            break

    entry = existing if existing is not None else {}
    entry["role"] = resolved_role
    entry["agent"] = agent
    entry["session_id"] = session_id
    entry["tmux_session"] = tmux_session
    entry["parent"] = parent_session_id
    entry["level"] = level  # int, or None when parent unresolvable
    entry["resolved"] = True
    entry["note"] = note
    if existing is None:
        knights.append(entry)

    try:
        _atomic_write_meta(meta_file, document)
    except Exception as exc:  # noqa: BLE001 - write must never crash the spawn
        return False, f"journey meta write failed ({meta_file}): {exc}"

    return True, level_warning
