"""The governed cascade — read from ``journeys/*/meta.json`` (the authoritative
command tree, NOT scan_knights' sparse parent ids), then enriched with live
state (``alive`` / ``context_pct``) joined on ``session_id`` from the motor.

This is a faithful Python port of the proven Rust logic in ``tui/src/cascade.rs``.
Two meta.json shapes exist in the wild:

* **New format** (``journey-tui``): a top-level ``tree_root`` (the King) plus a
  per-knight ``parent`` (the commanding session) and ``level``. Knights nest
  under the entry whose ``session_id == parent`` giving the true L1->L2->L3
  hierarchy. A knight whose parent is not found (dangling) hangs under the root
  so it is never dropped.
* **Old format** (other journeys): no ``tree_root``/``parent``/``level``, only a
  ``king_session``. The tree is rooted at ``king_session`` and every knight
  hangs directly under it (implicit L2). No deeper edges are invented.

Per-journey ``tui`` tag: ``ENABLED`` (or MISSING) shows the journey; any other
value (case-insensitive) hides the whole subtree.

This module is pure-ish and unit-testable: filesystem reads and the motor lookup
are injected, so tests pass sample data with zero I/O and zero motor import.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from models import CascadeNode, JourneyTree

# A "live index" maps session_id -> {"alive": bool, "context_pct": float|None}.
LiveIndex = Mapping[str, Mapping[str, Any]]


def _root_session(meta: Mapping[str, Any]) -> Optional[str]:
    """Authoritative root id: tree_root -> new_king_session -> king_session."""
    tree_root = meta.get("tree_root")
    if isinstance(tree_root, Mapping):
        sid = tree_root.get("session_id")
        if sid:
            return str(sid)
    for key in ("new_king_session", "king_session"):
        sid = meta.get(key)
        if sid:
            return str(sid)
    return None


def _is_tui_enabled(meta: Mapping[str, Any]) -> bool:
    """Missing tag -> enabled; only an explicit non-ENABLED value hides it."""
    tag = meta.get("tui")
    if tag is None:
        return True
    return str(tag).strip().lower() == "enabled"


def _leaf(knight: Mapping[str, Any]) -> CascadeNode:
    return CascadeNode(
        session_id=str(knight.get("session_id", "")),
        role=knight.get("role"),
        agent=knight.get("agent"),
        tmux_session=knight.get("tmux_session"),
        level=knight.get("level"),
    )


def _knight_is_tui_enabled(knight: Mapping[str, Any]) -> bool:
    """A per-knight ``tui`` tag can hide a single knight (e.g. a retired coder).

    Same rule as the journey tag: missing = enabled, only an explicit
    non-ENABLED value hides it.
    """
    tag = knight.get("tui")
    if tag is None:
        return True
    return str(tag).strip().lower() == "enabled"


def _build_children(
    parent_sid: str,
    by_parent: dict[str, list[Mapping[str, Any]]],
) -> list[CascadeNode]:
    """Recursively collect children of ``parent_sid``, consuming them from
    ``by_parent`` so a knight can never be attached twice."""
    direct = by_parent.pop(parent_sid, None)
    if not direct:
        return []
    nodes: list[CascadeNode] = []
    for knight in direct:
        node = _leaf(knight)
        node.children = _build_children(node.session_id, by_parent)
        nodes.append(node)
    return nodes


def _contains(node: CascadeNode, sid: str) -> bool:
    if node.session_id == sid:
        return True
    return any(_contains(child, sid) for child in node.children)


def parse_journey(meta: Mapping[str, Any]) -> Optional[JourneyTree]:
    """Parse one meta.json document into a :class:`JourneyTree`.

    Returns ``None`` when the journey is TUI-disabled or has no resolvable root
    (nothing to anchor a tree on). Raises nothing on structure — a malformed
    entry simply contributes an empty-ish leaf; the caller guards file I/O.
    """
    if not _is_tui_enabled(meta):
        return None

    root_sid = _root_session(meta)
    if not root_sid:
        return None

    knights_raw = meta.get("knights")
    knights: list[Mapping[str, Any]] = [
        k for k in (knights_raw or [])
        if isinstance(k, Mapping) and _knight_is_tui_enabled(k)
    ]

    has_parent_links = any(k.get("parent") for k in knights)

    tree_root = meta.get("tree_root")
    root_role = None
    if isinstance(tree_root, Mapping):
        root_role = tree_root.get("role")
    root = CascadeNode(
        session_id=root_sid,
        role=root_role or "king",
        level=1,
    )

    if has_parent_links:
        # New format: index by parent, nest recursively.
        by_parent: dict[str, list[Mapping[str, Any]]] = {}
        orphans: list[Mapping[str, Any]] = []
        for knight in knights:
            if str(knight.get("session_id", "")) == root_sid:
                continue  # a meta may list the king itself; skip
            parent = knight.get("parent")
            if parent:
                by_parent.setdefault(str(parent), []).append(knight)
            else:
                orphans.append(knight)
        root.children = _build_children(root_sid, by_parent)
        # Dangling parents (parent id not present) must not be dropped.
        for leftover in by_parent.values():
            for knight in leftover:
                root.children.append(_leaf(knight))
        # Orphan knights (no parent field) also hang under the root.
        for knight in orphans:
            if not _contains(root, str(knight.get("session_id", ""))):
                root.children.append(_leaf(knight))
    else:
        # Old format: every knight is an implicit L2 child of the King.
        for knight in knights:
            if str(knight.get("session_id", "")) != root_sid:
                root.children.append(_leaf(knight))

    return JourneyTree(journey_id=str(meta.get("journey_id", "")), root=root)


def _uuid_suffix(session_id: str) -> str:
    """The first 8 hex chars of a ``sess_<uuid>`` id — the token tmux session
    names embed (e.g. ``sess_354dd79f-...`` -> ``354dd79f`` -> ``king-354dd79f``).
    """
    body = session_id[len("sess_"):] if session_id.startswith("sess_") else session_id
    return body[:8]


def _resolve_tmux_by_id(session_id: str, live_tmux: frozenset[str]) -> Optional[str]:
    """Find a LIVE tmux session that belongs to ``session_id`` by matching the
    id's 8-hex suffix in the session name. Used for nodes (the king) whose
    meta.json omits an explicit ``tmux_session``. Returns a unique match only —
    an ambiguous match (two live sessions carry the suffix) resolves to None to
    avoid attaching to the wrong one.
    """
    suffix = _uuid_suffix(session_id)
    if not suffix:
        return None
    matches = [name for name in live_tmux if suffix in name]
    return matches[0] if len(matches) == 1 else None


def _enrich(node: CascadeNode, live: LiveIndex, live_tmux: frozenset[str]) -> None:
    """Attach display state (motor ``alive`` / ``context_pct``) and compute the
    two action flags (``clickable`` / ``revivable``).

    Attachability is tmux reality, NOT the motor's work-liveness (an idle but
    live knight is still attachable). The motor's ``alive``/``context_pct`` stay
    purely informational (the pulsing "working" cue).

    * If meta.json gives no ``tmux_session`` (the king), resolve it from the live
      tmux sessions by the id suffix so a running king becomes ``clickable``.
    * ``clickable``  = the resolved ``tmux_session`` is live now (switch).
    * ``revivable``  = no live tmux session but a usable ``session_id`` (revive
      via motor ``resume_clean`` on click). Never both.
    """
    entry = live.get(node.session_id)
    if entry is not None:
        node.alive = bool(entry.get("alive", False))
        ctx = entry.get("context_pct")
        node.context_pct = float(ctx) if ctx is not None else None

    # Resolve a missing tmux_session (king) from live sessions by id suffix.
    if not node.tmux_session and node.session_id:
        resolved = _resolve_tmux_by_id(node.session_id, live_tmux)
        if resolved:
            node.tmux_session = resolved

    tmux_live = bool(node.tmux_session) and node.tmux_session in live_tmux
    node.clickable = tmux_live
    # Revivable: we have a session id to resume, but no live tmux pane for it.
    # (A king resolved purely by suffix with no stored name is NOT revivable —
    # we only revive knights whose id maps to a resumable kiro session.)
    node.revivable = (not tmux_live) and bool(node.session_id)

    for child in node.children:
        _enrich(child, live, live_tmux)


def build_cascade(
    metas: list[Mapping[str, Any]],
    live: LiveIndex,
    live_tmux: Optional[frozenset[str]] = None,
) -> list[JourneyTree]:
    """Build the full governed cascade from parsed meta docs + a live index.

    Pure function (no I/O, no motor): the engine of the module, trivially
    unit-testable. ``metas`` are already-parsed meta.json mappings; ``live`` is
    a ``session_id -> {alive, context_pct}`` map; ``live_tmux`` is the set of
    tmux session names that exist right now (gates ``clickable``).
    """
    sessions = live_tmux if live_tmux is not None else frozenset()
    trees: list[JourneyTree] = []
    for meta in metas:
        tree = parse_journey(meta)
        if tree is None:
            continue
        _enrich(tree.root, live, sessions)
        trees.append(tree)
    trees.sort(key=lambda t: t.journey_id)
    return trees


def _find_node(node: CascadeNode, session_id: str) -> Optional[CascadeNode]:
    if node.session_id == session_id:
        return node
    for child in node.children:
        found = _find_node(child, session_id)
        if found is not None:
            return found
    return None


def resolve_node(trees: list[JourneyTree], session_id: str) -> Optional[CascadeNode]:
    """Find the governed node for ``session_id`` across all journeys (or None)."""
    for tree in trees:
        found = _find_node(tree.root, session_id)
        if found is not None:
            return found
    return None


# --- I/O + motor glue (kept thin, injected into the pure core above) ---------

def read_meta_files(root_dir: Path) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Read every ``journeys/*/meta.json`` under ``root_dir``.

    A single malformed file is collected as an error and skipped (one bad
    meta.json must never blank the whole panel). Fails loud only on an
    unreadable ``journeys/`` directory being absent is tolerated (returns empty).
    """
    metas: list[Mapping[str, Any]] = []
    errors: list[str] = []
    journeys_dir = root_dir / "journeys"
    if not journeys_dir.is_dir():
        return metas, [f"journeys dir not found: {journeys_dir}"]
    for meta_path in sorted(journeys_dir.glob("*/meta.json")):
        try:
            text = meta_path.read_text(encoding="utf-8")
            doc = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{meta_path}: {exc}")
            continue
        if isinstance(doc, Mapping):
            metas.append(doc)
        else:
            errors.append(f"{meta_path}: top-level JSON is not an object")
    return metas, errors


def live_index_from_scan(scan_result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Project a ``scan_knights()`` result into a session_id -> live map."""
    index: dict[str, dict[str, Any]] = {}
    for knight in scan_result.get("knights", []) or []:
        sid = knight.get("session_id")
        if not sid:
            continue
        index[str(sid)] = {
            "alive": bool(knight.get("alive", False)),
            "context_pct": knight.get("context_pct"),
        }
    return index


def list_live_tmux_sessions(tmux_bin: str = "tmux") -> frozenset[str]:
    """Return the set of tmux session names that exist right now.

    Ground truth for "is this knight attachable". A missing/failed tmux (no
    server running, binary absent) yields an EMPTY set rather than raising, so a
    tmux hiccup dims rows instead of blanking the whole panel. The call is
    bounded by an explicit timeout.
    """
    try:
        res = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [tmux_bin, "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return frozenset()
    if res.returncode != 0:
        # tmux returns non-zero with "no server running" when nothing is up.
        return frozenset()
    return frozenset(
        line.strip() for line in res.stdout.splitlines() if line.strip()
    )


def load_cascade(
    root_dir: Path,
    scan_knights: Callable[..., Mapping[str, Any]],
    tmux_bin: str = "tmux",
) -> tuple[list[JourneyTree], list[str]]:
    """Top-level loader: read meta files + the motor scan + live tmux sessions,
    then build the enriched cascade.

    ``root_dir`` is the Realm root (where ``journeys/*/meta.json`` live).
    ``scan_knights`` is injected (the motor function in production, a stub in
    tests) so this module never imports the motor directly. It is called with NO
    root so the motor uses its DEFAULT sessions root — the kiro session store is
    a different location from the journeys dir, so passing ``root_dir`` here
    would make the motor scan the wrong tree.

    ``clickable`` is gated on live tmux sessions (``list_live_tmux_sessions``),
    not on the motor's work-liveness, so idle-but-attachable knights stay
    switchable.
    """
    metas, errors = read_meta_files(root_dir)
    scan = scan_knights()
    live = live_index_from_scan(scan)
    live_tmux = list_live_tmux_sessions(tmux_bin)
    trees = build_cascade(metas, live, live_tmux)
    return trees, errors
