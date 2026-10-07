"""Bootstrap a fresh wise-king journey from the browser.

Pure-ish core (slugify, meta shape, id parsing) is unit-testable with zero I/O;
the motor/tmux/filesystem effects are injected so tests never spawn a real king.

Boot the king at ROOT_DIR (~/tmux-mainbrain) so it can drive the motor, in a
tmux session NAMED AFTER THE JOURNEY (slugified to a tmux-safe name).

Session-id capture (verified empirically 2026-10-03): kiro-cli has NO
``/session-id`` slash command. The id is printed in the STARTUP BANNER as:

    • Session ID: sess_<uuid>
      Resume with: kiro-cli --resume-id sess_<uuid>

So capture order is: (1) the spawn result's resolved ``session_id`` (proven
exact), (2) parse ``Session ID: sess_...`` from the pane, (3) ``scan_knights``
matched on the new tmux session. Fail loud + clean up if ALL fail.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# tmux session names cannot contain '.' or ':' and whitespace is awkward; keep
# it to [a-z0-9-]. Journey ids follow the same slug for a 1:1 name.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SESSION_ID_RE = re.compile(r"\bsess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")

PARENTING_RULE = (
    "parent = who COMMANDS this knight (the requester), never who FORGED it. "
    "The Steward is a forge, not a parent: when X asks the Steward to create a "
    "knight, X receives the knight data and writes it here with parent=X."
)


class JourneyError(RuntimeError):
    """A journey bootstrap failed (collision, spawn, or id-capture)."""


def slugify(name: str) -> str:
    """Name -> tmux-safe, filesystem-safe slug ([a-z0-9-], no leading/trailing
    or doubled '-'). Raises JourneyError on an empty/invalid result."""
    if not name or not name.strip():
        raise JourneyError("journey name must not be empty")
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise JourneyError(f"journey name {name!r} has no usable characters")
    return slug


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_session_id(text: str) -> Optional[str]:
    """Extract the first ``sess_<uuid>`` from kiro banner / pane text, else None."""
    if not text:
        return None
    m = _SESSION_ID_RE.search(text)
    return m.group(0) if m else None


def build_meta(journey_id: str, goal: str, king_session: str,
               king_tmux: Optional[str] = None) -> dict[str, Any]:
    """The initial meta.json, mirroring the proven journey-tui shape.

    ``king_tmux`` (when given) is recorded in ``tree_root.tmux_session`` so the
    cascade resolves + clickable-marks the king directly, no convention guess.
    """
    now = _now_iso()
    tree_root: dict[str, Any] = {
        "role": "king", "level": 1, "session_id": king_session,
    }
    if king_tmux:
        tree_root["tmux_session"] = king_tmux
    return {
        "journey_id": journey_id,
        "tui": "ENABLED",
        "goal": goal or "",
        "king_session": king_session,
        "king_resume": f"kiro-cli --v3 chat --resume-id {king_session}",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "parenting_rule": PARENTING_RULE,
        "tree_root": tree_root,
        "knights": [],
    }


def atomic_write_json(path: Path, doc: Mapping[str, Any]) -> None:
    """Write JSON atomically (temp in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".meta-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_journey(
    name: str,
    root_dir: Path,
    *,
    spawn: Callable[..., Mapping[str, Any]],
    session_exists: Callable[[str], bool],
    capture_pane: Callable[[str], str],
    scan_knights: Callable[[], Mapping[str, Any]],
    kill_session: Callable[[str], None],
) -> dict[str, Any]:
    """Create journey dirs, spawn a wise-king named after the journey, capture
    its session id, write meta.json. All effects injected (testable).

    Returns {journey_id, tmux_session, king_session}. Raises JourneyError on
    collision (nothing created) or on a spawn/id failure (half-created journey
    dir + spawned tmux are cleaned up so no orphans remain).
    """
    journey_id = slugify(name)
    # The king's tmux session follows the convention king-<journey_id> so the
    # cascade can resolve + clickable-mark it reliably (recorded in meta too).
    session_name = f"king-{journey_id}"

    journey_dir = root_dir / "journeys" / journey_id

    # 1. Collision checks — reject, never auto-suffix. Create nothing.
    if journey_dir.exists():
        raise JourneyError(f"journey '{journey_id}' already exists")
    if session_exists(session_name):
        raise JourneyError(f"tmux session '{session_name}' already exists")

    # 2. Create dirs.
    artifacts = journey_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=False)

    spawned = False
    try:
        # 3. Spawn the king at ROOT_DIR (so it can drive the motor).
        result = spawn("wise-king", tmux_session=session_name, cwd=str(root_dir))
        spawned = True

        # 4. Capture the king's session id (ordered fallbacks).
        king_session = _capture_king_session(
            result, session_name, capture_pane, scan_knights
        )
        if not king_session:
            raise JourneyError(
                "could not determine the king's session id by any method"
            )

        # 5. Write meta.json atomically.
        atomic_write_json(journey_dir / "meta.json",
                          build_meta(journey_id, goal=name,
                                     king_session=king_session,
                                     king_tmux=session_name))
    except Exception as exc:
        # Clean up: no orphan journey dir, no orphan tmux session.
        if spawned:
            try:
                kill_session(session_name)
            except Exception:  # noqa: BLE001 - best-effort cleanup, surface original
                pass
        shutil.rmtree(journey_dir, ignore_errors=True)
        if isinstance(exc, JourneyError):
            raise
        raise JourneyError(f"journey bootstrap failed: {exc}") from exc

    return {
        "journey_id": journey_id,
        "tmux_session": session_name,
        "king_session": king_session,
    }


def _capture_king_session(
    spawn_result: Mapping[str, Any],
    session_name: str,
    capture_pane: Callable[[str], str],
    scan_knights: Callable[[], Mapping[str, Any]],
) -> Optional[str]:
    """Resolve the king's session id: spawn result -> pane banner -> scan."""
    # (1) spawn result when it resolved (proven exact match).
    if spawn_result.get("resolved") and spawn_result.get("session_id"):
        return str(spawn_result["session_id"])

    # (2) parse the startup banner from the pane ("• Session ID: sess_...").
    try:
        sid = parse_session_id(capture_pane(session_name))
        if sid:
            return sid
    except Exception:  # noqa: BLE001 - fall through to scan
        pass

    # (3) scan_knights matched on the new tmux session.
    try:
        scan = scan_knights()
        for knight in scan.get("knights", []) or []:
            if knight.get("tmux_session") == session_name and knight.get("session_id"):
                return str(knight["session_id"])
    except Exception:  # noqa: BLE001 - all methods exhausted
        pass
    return None
