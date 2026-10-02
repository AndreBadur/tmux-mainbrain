"""The tmux driver — spawn / resume / deliver / watch / peek (POCs 02 + 08).

The Steward's *runtime* hands. Every subprocess call is bounded by an explicit
timeout so the motor NEVER hangs forever (Phase 1 design rule). Payload delivery
uses ``load-buffer`` -> ``paste-buffer -p`` -> ``Enter`` (POC 02 proved
send-keys caps at ~16k chars; load/paste is effectively unlimited). The ``-p``
flag emits bracketed-paste markers so the kiro-cli V3 TUI treats a multi-line
payload as ONE input instead of submitting at each newline (the newline trap).

Readiness / completion are detected from the pane text (POC 08):
  BUSY  -> "Kiro is working"
  IDLE  -> "ask a question or describe a task"

All functions are deterministic mechanics: no AI, no prompt interpretation.

Testability seams (review iter-1, item 6): the completion/resolution *logic* is
pure — it operates on a pane string and a session-id set. ``watch`` and
``_resolve_new_session_id`` accept injectable ``capture_fn`` / ``sleep_fn`` /
``list_ids_fn`` so the false-positive and ambiguous-spawn cases are unit-tested
WITHOUT a real tmux or kiro-cli.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .errors import DeliveryError, TmuxError, TmuxTimeoutError, SessionNotFoundError
from .lineage import record_lineage
from . import lockcheck

# --- pane-state indicators (POC 07/08) --------------------------------------
_IDLE_MARKER = "ask a question or describe a task"
_BUSY_MARKER = "Kiro is working"
# Ready gauge matched STRUCTURALLY (mid-dot + spinner glyph + percentage), NOT
# on the model label — the middle token is model-dependent (review C1 MINOR).
_READY_GAUGE = re.compile(r"·\s*[◔◑◕○●◐]?\s*\d+%")

# --- default timeouts / cadences (seconds); all bounded, never infinite -----
_CMD_TIMEOUT = 15            # any single tmux command
_SPAWN_READY_TIMEOUT = 90    # kiro-cli v3 TUI cold start
_WATCH_TIMEOUT = 600         # default completion wait
_POLL_INTERVAL = 1.5         # poll cadence for ready/watch loops
_CAPTURE_LINES = 200         # lines captured when probing pane state
_SETTLE_SECONDS = 3.0        # post-deliver settle before watching (was <=1.0)
_IDLE_STABLE_SAMPLES = 3     # consecutive stable idle samples => done (no-busy path)
_SESSION_ID_TIMEOUT = 15     # bound for resolving the new sess_* id

# Type aliases for the injectable seams.
CaptureFn = Callable[[str, int], str]      # (tmux_session, lines) -> pane text
SleepFn = Callable[[float], None]          # (seconds) -> None
NowFn = Callable[[], float]                # -> monotonic seconds
ListIdsFn = Callable[[], set]              # -> current sess_* id set


# --------------------------------------------------------------------------- #
# low-level tmux command runner (single choke point for timeouts + errors)
# --------------------------------------------------------------------------- #
def _tmux(*args: str, timeout: int = _CMD_TIMEOUT,
          input_bytes: Optional[bytes] = None) -> str:
    """Run one ``tmux`` command with a hard timeout; return stdout (text)."""
    cmd = ["tmux", *args]
    try:
        completed = subprocess.run(
            cmd,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TmuxError("tmux binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TmuxTimeoutError(f"tmux {args[0]} timed out after {timeout}s") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise TmuxError(f"tmux {' '.join(args)} failed ({completed.returncode}): {stderr}")
    return completed.stdout.decode("utf-8", "replace")


def _session_exists(tmux_session: str) -> bool:
    """True if the tmux session is present. Guard clause helper."""
    try:
        _tmux("has-session", "-t", tmux_session)
        return True
    except TmuxError:
        return False


# --------------------------------------------------------------------------- #
# secret env injection (.env.secrets -> tmux new-session -e KEY=VAL)
# --------------------------------------------------------------------------- #
def _secret_env_flags() -> list[str]:
    """Parse ``<repo>/.env.secrets`` into tmux ``-e KEY=VAL`` flags.

    Null-tolerant by design: if the file is absent (a host that relies on the
    global MCP config, or a machine that has not created it yet), returns an
    empty list — spawn/resume proceed unchanged, NEVER crash. Lines may be
    ``export KEY=VAL`` or ``KEY=VAL``; blanks and ``#`` comments are ignored.
    The VALUE may itself contain ``=`` (e.g. base64 tokens / JSON) — only the
    FIRST ``=`` splits key from value. These flags load the secrets into the
    kiro-cli child environment so the nexus plugins' ``os.environ.get(...)`` can
    read them when no mcp.json ``env`` block supplies the key.
    """
    from .paths import repo_root  # lazy import: avoid load-time cycle

    env_path = repo_root() / ".env.secrets"
    try:
        raw = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []  # fallback file absent -> no-op, deterministic

    flags: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        flags.extend(["-e", f"{key}={value}"])
    return flags


# --------------------------------------------------------------------------- #
# pane-state classification (pure helpers — the testable core)
# --------------------------------------------------------------------------- #
def _pane_is_busy(pane: str) -> bool:
    return _BUSY_MARKER in pane


def _pane_is_idle(pane: str) -> bool:
    return _BUSY_MARKER not in pane and _IDLE_MARKER in pane


def _pane_is_ready(pane: str) -> bool:
    """Ready to accept input: idle prompt visible or the structural gauge."""
    return _IDLE_MARKER in pane or bool(_READY_GAUGE.search(pane))


def _pane_hash(pane: str) -> str:
    return hashlib.sha256(pane.encode("utf-8", "replace")).hexdigest()


# --------------------------------------------------------------------------- #
# peek (POC 02): on-demand pane read, no copy into any state file
# --------------------------------------------------------------------------- #
def peek(tmux_session: str, lines: int = _CAPTURE_LINES) -> str:
    """Capture the last ``lines`` of a pane (``capture-pane -p -S -N``)."""
    if lines <= 0:
        raise ValueError("lines must be positive")
    if not _session_exists(tmux_session):
        raise TmuxError(f"tmux session '{tmux_session}' does not exist")
    return _tmux("capture-pane", "-t", tmux_session, "-p", "-S", f"-{lines}")


# --------------------------------------------------------------------------- #
# deliver (POCs 02/08): load-buffer -> paste-buffer -> Enter, unlimited payload
# --------------------------------------------------------------------------- #
def deliver(tmux_session: str, prompt: str, require_ready: bool = True) -> None:
    """Deliver an arbitrarily large prompt to a session, then press Enter.

    review iter-1 (C1 MAJOR): by DEFAULT (``require_ready=True``) the pane is
    asserted to be at a ready/idle input state before pasting — this closes the
    "delivered nothing but the loop succeeded" hole. Callers may opt out with
    ``require_ready=False``, in which case they MUST ``watch``-for-busy after
    delivering to confirm acceptance (documented contract).
    """
    if not prompt:
        raise DeliveryError("refusing to deliver an empty prompt")
    if not _session_exists(tmux_session):
        raise DeliveryError(f"tmux session '{tmux_session}' does not exist")

    if require_ready:
        pane = peek(tmux_session, lines=_CAPTURE_LINES)
        if _pane_is_busy(pane) or not _pane_is_ready(pane):
            raise DeliveryError(
                f"pane '{tmux_session}' is not at a ready input state; "
                "refusing to paste (busy or non-idle). "
                "Pass require_ready=False only if you watch-for-busy afterward."
            )

    buffer_name = f"motor-{uuid.uuid4().hex[:12]}"
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".prompt", delete=False
        ) as handle:
            handle.write(prompt)
            tmp_path = handle.name

        _tmux("load-buffer", "-b", buffer_name, tmp_path)
        # ``-p`` emits bracketed-paste markers (ESC[200~ … ESC[201~) so the
        # kiro-cli V3 TUI treats the whole payload as ONE multi-line input
        # instead of submitting at each embedded newline (the "newline trap").
        # Verified live: without -p a multi-line prompt fragments into N queued
        # messages; with -p it lands as a single block, then one Enter submits.
        _tmux("paste-buffer", "-p", "-b", buffer_name, "-t", tmux_session)
        _tmux("send-keys", "-t", tmux_session, "Enter")
    except TmuxError as exc:
        raise DeliveryError(f"delivery to '{tmux_session}' failed: {exc}") from exc
    finally:
        try:
            _tmux("delete-buffer", "-b", buffer_name)
        except TmuxError:
            pass
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# spawn (POC 08): new tmux + kiro-cli --v3, wait ready, capture kiro session-id
# --------------------------------------------------------------------------- #
def spawn(agent: str,
          tmux_session: Optional[str] = None,
          cwd: Optional[str] = None,
          ready_timeout: int = _SPAWN_READY_TIMEOUT,
          parent: Optional[str] = None,
          journey: Optional[str] = None,
          role: Optional[str] = None) -> dict[str, object]:
    """Create a detached tmux session running ``kiro-cli --v3 chat --agent X``.

    review iter-1 (C3 MAJOR): the session-id snapshot is SCOPED to the spawn's
    own ``cwd`` workspace hash (``sha256(cwd)[:16]``) so concurrent spawns in
    other workspaces cannot create ambiguity. If the id cannot be uniquely
    resolved (kiro slow to write, or >1 new id in this workspace), the result
    is EXPLICIT: ``{session_id: None, resolved: False}`` — never a silent "".
    The Steward must then resolve via ``scan_knights`` before trusting the id.

    Lineage (optional, backward-compatible): when BOTH ``parent`` (the
    COMMANDING knight's session id) and ``journey`` are given AND the spawn
    resolves a session id, the new knight is appended/updated in that journey's
    ``meta.json`` ``knights[]`` with ``parent`` set and ``level`` = parent level
    + 1 (see :func:`motor.lineage.record_lineage`). This is FAIL-SOFT — a
    missing/corrupt journey file never fails the spawn; the result carries
    ``lineage_recorded`` (bool) and, when False, ``lineage_warning``. When the
    args are absent the spawn behaves EXACTLY as before (nothing recorded).

    Returns {tmux_session, agent, session_id, resolved, lineage_recorded[,
    lineage_warning]}.
    """
    if not agent or not agent.strip():
        raise ValueError("agent must be a non-empty string")

    session_name = tmux_session or f"knight-{agent}-{uuid.uuid4().hex[:6]}"
    if _session_exists(session_name):
        raise TmuxError(f"tmux session '{session_name}' already exists")

    work_dir = cwd or str(Path.cwd())
    workspace_hash = _workspace_hash(work_dir)
    before = _snapshot_session_ids(workspace_hash)
    launch = f"kiro-cli --v3 chat --agent {agent}"

    _tmux("new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", "-c",
          work_dir, *_secret_env_flags(), launch)
    _wait_until_ready(session_name, ready_timeout)

    session_id = _resolve_new_session_id(
        before,
        timeout=_SESSION_ID_TIMEOUT,
        list_ids_fn=lambda: _snapshot_session_ids(workspace_hash),
    )
    result: dict[str, object] = {
        "tmux_session": session_name,
        "agent": agent,
        "session_id": session_id,             # None when unresolved (not "")
        "resolved": session_id is not None,
    }
    _apply_lineage(result, session_id, session_name, agent, parent, journey, role)
    return result


# --------------------------------------------------------------------------- #
# resume (POC 08): rebuild a dead runtime from a durable kiro session-id
# --------------------------------------------------------------------------- #
def resume(kiro_session_id: str,
           tmux_session: Optional[str] = None,
           cwd: Optional[str] = None,
           ready_timeout: int = _SPAWN_READY_TIMEOUT,
           parent: Optional[str] = None,
           journey: Optional[str] = None,
           role: Optional[str] = None,
           agent: Optional[str] = None) -> dict[str, object]:
    """Create a tmux session running ``kiro-cli --v3 chat --resume-id X``.

    Lineage (optional, backward-compatible): mirrors :func:`spawn`. When BOTH
    ``parent`` and ``journey`` are given the resumed knight (its session id IS
    ``kiro_session_id``) is appended/updated in that journey's ``meta.json``.
    FAIL-SOFT; result carries ``lineage_recorded`` (+ ``lineage_warning`` when
    False). ``agent`` is optional metadata for the recorded entry (defaults to
    an empty string; ``role`` then falls back to it). Absent lineage args ->
    behaves EXACTLY as before.

    Returns {tmux_session, session_id, resolved, lineage_recorded[,
    lineage_warning]}.
    """
    if not kiro_session_id or not kiro_session_id.strip():
        raise ValueError("kiro_session_id must be a non-empty string")

    return _resume_core(kiro_session_id, tmux_session=tmux_session, cwd=cwd,
                        ready_timeout=ready_timeout, parent=parent,
                        journey=journey, role=role, agent=agent)


def _resume_core(kiro_session_id: str,
                 tmux_session: Optional[str] = None,
                 cwd: Optional[str] = None,
                 ready_timeout: int = _SPAWN_READY_TIMEOUT,
                 parent: Optional[str] = None,
                 journey: Optional[str] = None,
                 role: Optional[str] = None,
                 agent: Optional[str] = None,
                 extra: Optional[dict[str, object]] = None) -> dict[str, object]:
    """Shared resume mechanic: launch tmux + ``--resume-id`` + wait-ready +
    optional lineage. Both :func:`resume` and :func:`resume_clean` call this so
    the tmux-launch+wait-ready+lineage logic is written ONCE (no duplication).

    ``extra`` merges caller-specific keys (e.g. ``removed_stale_lock``) into the
    returned dict without the core needing to know about them.
    """
    session_name = tmux_session or f"knight-resume-{uuid.uuid4().hex[:6]}"
    if _session_exists(session_name):
        raise TmuxError(f"tmux session '{session_name}' already exists")

    work_dir = cwd or str(Path.cwd())
    launch = f"kiro-cli --v3 chat --resume-id {kiro_session_id}"
    _tmux("new-session", "-d", "-s", session_name, "-x", "220", "-y", "50", "-c",
          work_dir, *_secret_env_flags(), launch)
    _wait_until_ready(session_name, ready_timeout)
    result: dict[str, object] = {
        "tmux_session": session_name,
        "session_id": kiro_session_id,
        "resolved": True,
    }
    if extra:
        result.update(extra)
    _apply_lineage(result, kiro_session_id, session_name, agent or "",
                   parent, journey, role)
    return result


def _apply_lineage(result: dict[str, object],
                   session_id: Optional[str],
                   tmux_session: str,
                   agent: str,
                   parent: Optional[str],
                   journey: Optional[str],
                   role: Optional[str]) -> None:
    """Fold optional parent-lineage recording into a spawn/resume result dict.

    Backward-compatible contract:
      * lineage args absent (no ``parent`` AND no ``journey``) -> record nothing;
        set ``lineage_recorded=False`` with NO ``lineage_warning`` (unchanged
        shape apart from the always-present boolean flag).
      * only one of the pair given, or the session id did not resolve -> a
        ``lineage_warning`` explains why nothing was recorded.
      * both given AND a session id -> delegate to the FAIL-SOFT
        :func:`motor.lineage.record_lineage`; never raises, never hangs.
    """
    if not parent and not journey:
        result["lineage_recorded"] = False
        return

    if not (parent and journey):
        missing = "journey" if parent else "parent"
        result["lineage_recorded"] = False
        result["lineage_warning"] = (
            f"lineage needs BOTH --parent and --journey; missing --{missing}"
        )
        return

    if not session_id:
        result["lineage_recorded"] = False
        result["lineage_warning"] = (
            "session id unresolved; lineage not recorded (resolve via "
            "scan_knights, then re-record)"
        )
        return

    recorded, warning = record_lineage(
        journey_id=journey,
        session_id=session_id,
        tmux_session=tmux_session,
        agent=agent,
        parent_session_id=parent,
        role=role,
    )
    result["lineage_recorded"] = recorded
    if warning:
        result["lineage_warning"] = warning


# --------------------------------------------------------------------------- #
# resume_clean: stale-lock-aware resume (kiro has no native lock/force flag)
# --------------------------------------------------------------------------- #
def resume_clean(kiro_session_id: str,
                 tmux_session: Optional[str] = None,
                 cwd: Optional[str] = None,
                 ready_timeout: int = _SPAWN_READY_TIMEOUT,
                 parent: Optional[str] = None,
                 journey: Optional[str] = None,
                 role: Optional[str] = None,
                 agent: Optional[str] = None,
                 root: Optional[Path] = None,
                 pid_alive_fn: Optional[lockcheck.PidAliveFn] = None
                 ) -> dict[str, object]:
    """Resume a session, first clearing a STALE (dead-owner) ``.lock`` if any.

    kiro-cli has no lock/force flag, so a ``.lock`` left by a DEAD process makes
    a plain :func:`resume` hang on "Initializing". This verb inspects the lock
    (via :mod:`motor.lockcheck`, which resolves the lock path from the session's
    ``source_path`` — nothing hardcoded) and applies the safe decision table:

      * NO lock            -> resume straight away (``removed_stale_lock=False``).
      * lock, DEAD pid     -> remove the stale ``.lock``, then resume
                              (``removed_stale_lock=True``).
      * lock, ALIVE pid    -> ⚠ REFUSE: do NOT delete, do NOT kill, do NOT
                              resume. ``{resumed: False, reason: 'held_by_live',
                              pid, tmux_session: None}``. NON-NEGOTIABLE — never
                              clear a lock whose owner is provably alive.
      * pid unparseable /  -> REFUSE (safer than guessing): ``{resumed: False,
        lock unreadable        reason: 'unknown_lock', tmux_session: None}``.

    On the resume path the SAME mechanic as :func:`resume` runs (shared
    ``_resume_core``: tmux launch + ``--resume-id`` + wait-ready + optional
    lineage), so nothing is duplicated. Lineage is FAIL-SOFT.

    The pid-liveness probe is injectable (``pid_alive_fn``, default the real
    ``/proc`` check) so tests are deterministic without a real process.

    Returns:
        success -> {resumed: True, tmux_session, session_id, removed_stale_lock,
                    [pid], resolved, lineage_recorded[, lineage_warning]}
        refuse  -> {resumed: False, reason, tmux_session: None,
                    removed_stale_lock: False[, pid]}
    """
    if not kiro_session_id or not kiro_session_id.strip():
        raise ValueError("kiro_session_id must be a non-empty string")

    # 1) Inspect the lock. A missing session dir/file fails SOFT (clear reason).
    try:
        decision = lockcheck.inspect_lock(kiro_session_id, root=root,
                                          pid_alive_fn=pid_alive_fn)
    except SessionNotFoundError as exc:
        return {"resumed": False, "reason": "session_not_found",
                "tmux_session": None, "removed_stale_lock": False,
                "detail": str(exc)}

    state = decision["state"]

    # 2) Apply the safety decision table.
    if state == lockcheck.LOCK_LIVE:
        # NON-NEGOTIABLE: owner provably alive -> refuse, touch nothing.
        return {"resumed": False, "reason": lockcheck.LOCK_LIVE,
                "pid": decision.get("pid"), "tmux_session": None,
                "removed_stale_lock": False}

    if state == lockcheck.LOCK_UNKNOWN:
        # Unparseable pid / unreadable lock -> refuse rather than guess.
        return {"resumed": False, "reason": lockcheck.LOCK_UNKNOWN,
                "tmux_session": None, "removed_stale_lock": False}

    removed_stale_lock = False
    if state == lockcheck.LOCK_STALE:
        # Dead owner: the lock is stale -> remove it, then resume.
        try:
            Path(decision["lock_path"]).unlink()
            removed_stale_lock = True
        except FileNotFoundError:
            removed_stale_lock = True  # already gone; equivalent to cleared
        except OSError as exc:
            # Could not clear the stale lock -> refuse (resume would hang).
            return {"resumed": False, "reason": "stale_lock_unremovable",
                    "pid": decision.get("pid"), "tmux_session": None,
                    "removed_stale_lock": False, "detail": str(exc)}

    # 3) Clean lock state (absent or just-cleared): perform the shared resume.
    result = _resume_core(
        kiro_session_id, tmux_session=tmux_session, cwd=cwd,
        ready_timeout=ready_timeout, parent=parent, journey=journey,
        role=role, agent=agent,
        extra={"resumed": True, "removed_stale_lock": removed_stale_lock},
    )
    if state == lockcheck.LOCK_STALE:
        result["pid"] = decision.get("pid")
    return result


# --------------------------------------------------------------------------- #
# watch (POCs 02/08): completion via an OBSERVED transition, not a snapshot
# --------------------------------------------------------------------------- #
def watch(tmux_session: str,
          timeout: int = _WATCH_TIMEOUT,
          sentinel: Optional[str] = None,
          poll_interval: float = _POLL_INTERVAL,
          settle_seconds: float = _SETTLE_SECONDS,
          idle_stable_samples: int = _IDLE_STABLE_SAMPLES,
          capture_fn: Optional[CaptureFn] = None,
          sleep_fn: Optional[SleepFn] = None,
          now_fn: Optional[NowFn] = None) -> dict[str, object]:
    """Poll a pane until the agent has genuinely completed.

    review iter-1 (C1 BLOCKER): completion is a TRANSITION, not a snapshot.
    Idle is accepted as "done" only when EITHER

      * the BUSY marker was observed at least once since the watch started
        (a real BUSY -> IDLE transition), OR
      * ``idle_stable_samples`` consecutive idle samples are seen with the pane
        content hash UNCHANGED across them (the agent never rendered BUSY, e.g.
        an instant reply, but the output has clearly settled).

    A ``sentinel`` (when provided) still short-circuits, but to avoid the
    prompt-echo false-positive (POC 02 caveat) the sentinel is only accepted
    once the pane is NOT busy (the agent has stopped writing).

    Injectable seams (``capture_fn``/``sleep_fn``/``now_fn``) make this pure
    logic unit-testable with synthetic pane sequences. Defaults use real tmux.
    """
    capture = capture_fn or (lambda sess, lines: peek(sess, lines=lines))
    sleeper = sleep_fn or time.sleep
    clock = now_fn or time.monotonic

    if capture_fn is None and not _session_exists(tmux_session):
        raise TmuxError(f"tmux session '{tmux_session}' does not exist")

    started = clock()
    deadline = started + timeout
    sleeper(min(settle_seconds, timeout))

    seen_busy = False
    stable_idle_count = 0
    last_idle_hash: Optional[str] = None

    while clock() < deadline:
        pane = capture(tmux_session, _CAPTURE_LINES)
        busy = _pane_is_busy(pane)
        idle = _pane_is_idle(pane)

        if busy:
            seen_busy = True
            stable_idle_count = 0
            last_idle_hash = None

        if sentinel is not None:
            # Only trust the sentinel once the agent is not actively writing,
            # so the echoed prompt line cannot trigger a premature completion.
            if not busy and sentinel in pane:
                return _watch_result(True, "sentinel", started, clock)
        elif idle:
            if seen_busy:
                # A genuine BUSY -> IDLE transition: the turn finished.
                return _watch_result(True, "idle-transition", started, clock)
            # No busy ever seen: require stable, unchanged idle output.
            current_hash = _pane_hash(pane)
            if current_hash == last_idle_hash:
                stable_idle_count += 1
            else:
                stable_idle_count = 1
                last_idle_hash = current_hash
            if stable_idle_count >= idle_stable_samples:
                return _watch_result(True, "idle-stable", started, clock)

        sleeper(poll_interval)

    raise TmuxTimeoutError(
        f"watch on '{tmux_session}' timed out after {timeout}s "
        f"(no {'sentinel' if sentinel else 'completion'} signal; "
        f"seen_busy={seen_busy})"
    )


def _watch_result(done: bool, reason: str, started: float,
                  clock: NowFn) -> dict[str, object]:
    return {"done": done, "reason": reason,
            "elapsed_seconds": round(clock() - started, 2)}


# --------------------------------------------------------------------------- #
# readiness + session-id resolution helpers
# --------------------------------------------------------------------------- #
def _wait_until_ready(tmux_session: str, timeout: int,
                      capture_fn: Optional[CaptureFn] = None,
                      sleep_fn: Optional[SleepFn] = None,
                      now_fn: Optional[NowFn] = None) -> None:
    """Block until the pane shows the idle prompt or the structural gauge."""
    capture = capture_fn or (lambda sess, lines: peek(sess, lines=lines))
    sleeper = sleep_fn or time.sleep
    clock = now_fn or time.monotonic

    deadline = clock() + timeout
    while clock() < deadline:
        try:
            pane = capture(tmux_session, _CAPTURE_LINES)
        except TmuxError:
            pane = ""  # pane not renderable yet in the first moments; retry
        if _pane_is_ready(pane):
            return
        sleeper(_POLL_INTERVAL)
    raise TmuxTimeoutError(f"session '{tmux_session}' not ready within {timeout}s")


def _workspace_hash(cwd: str) -> str:
    """kiro v3 workspace dir name = first 16 hex of sha256(cwd) (verified POC 08)."""
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]


def _snapshot_session_ids(workspace_hash: Optional[str] = None) -> set:
    """Set of v3 ``sess_*`` ids on disk, optionally scoped to one workspace.

    Scoping to the spawn's own workspace hash (C3 fix) means concurrent spawns
    in *other* cwds do not pollute this snapshot's before/after diff.
    """
    from .paths import sessions_root  # lazy import: avoid load-time cycle

    root = sessions_root()
    ids: set = set()
    if not root.exists():
        return ids

    if workspace_hash is not None:
        workspace_dirs = [root / workspace_hash]
    else:
        workspace_dirs = [d for d in root.iterdir()
                          if d.is_dir() and d.name != "cli"]

    for workspace_dir in workspace_dirs:
        if not workspace_dir.exists() or not workspace_dir.is_dir():
            continue
        for sess_dir in workspace_dir.glob("sess_*"):
            if (sess_dir / "session.json").exists():
                ids.add(sess_dir.name)
    return ids


def _resolve_new_session_id(before: set,
                            timeout: int,
                            list_ids_fn: Optional[ListIdsFn] = None,
                            sleep_fn: Optional[SleepFn] = None,
                            now_fn: Optional[NowFn] = None) -> Optional[str]:
    """Poll for the single new ``sess_*`` id created since ``before``.

    Returns the id only when EXACTLY ONE new session appeared (deterministic).
    Returns None (=> spawn reports resolved:false) when kiro has not yet
    written the dir within ``timeout``, or when >1 new id is ambiguous. Never
    guesses. Injectable ``list_ids_fn``/``sleep_fn``/``now_fn`` for tests.
    """
    lister = list_ids_fn or (lambda: _snapshot_session_ids())
    sleeper = sleep_fn or time.sleep
    clock = now_fn or time.monotonic

    deadline = clock() + timeout
    while clock() < deadline:
        new_ids = lister() - before
        if len(new_ids) == 1:
            return next(iter(new_ids))
        if len(new_ids) > 1:
            return None  # ambiguous: caller resolves via scan_knights later
        sleeper(_POLL_INTERVAL)
    return None
