"""Deterministic, isolated unit tests for the steward routing layer (Phase 3).

100% isolated: NO real tmux/kiro. ``route()`` is pure so most tests need no
fakes; ``execute_decision``/``inject_knowledge``/``delete`` are driven through a
fake :class:`StewardPort`.

Run:  python3 steward/tests/test_steward.py
  or: python3 -m pytest steward/tests -q
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from steward import (  # noqa: E402
    Action,
    StewardPort,
    assemble_payload,
    delete,
    execute_decision,
    inject_knowledge,
    keep,
    retire,
    route,
)
from steward.errors import (  # noqa: E402
    DeleteRefusedError,
    InjectionTooLargeError,
    SourceNotFoundError,
    StewardError,
)


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .isoformat().replace("+00:00", "Z")


def _strong(**over):
    base = {
        "session_id": "sess_strong", "agent": "wrcp",
        "essence": "ptp", "salons": ["ptp/packaging", "deployment/lab-ops"],
        "context_pct": 30.0, "window": 1_000_000, "alive": True,
        "last_seen": _iso_days_ago(1), "match_score": 0.9, "centrality": 0.9,
    }
    base.update(over)
    return base


TASK_SALONS = ["ptp/packaging", "deployment/lab-ops"]


# --------------------------------------------------------------------------- #
# route() decision tree
# --------------------------------------------------------------------------- #
def test_route_reuse_strong_alive_headroom():
    d = route(TASK_SALONS, [_strong()], capacity_policy={})
    assert d.action is Action.REUSE
    assert d.chosen_session_id == "sess_strong"
    assert "reuse" in d.reason.lower()


def test_route_resume_when_dead():
    d = route(TASK_SALONS, [_strong(alive=False)])
    assert d.action is Action.RESUME
    assert d.chosen_session_id == "sess_strong"


def test_route_extract_fresh_when_near_full():
    d = route(TASK_SALONS, [_strong(context_pct=85.0)])
    assert d.action is Action.EXTRACT_FRESH
    assert "near full" in d.reason.lower()


def test_route_extract_fresh_when_low_headroom():
    # Alive, strong, not near-full by the near_full cutoff, but headroom < min.
    d = route(TASK_SALONS, [_strong(context_pct=79.0)],
              capacity_policy={"near_full_pct": 95.0, "headroom_min_pct": 30.0})
    assert d.action is Action.EXTRACT_FRESH
    assert "headroom" in d.reason.lower()


def test_route_spawn_on_weak_match():
    weak = _strong(match_score=0.2, centrality=0.2)
    d = route(TASK_SALONS, [weak])
    assert d.action is Action.SPAWN
    assert d.chosen_session_id is None


def test_route_spawn_on_empty_candidates():
    d = route(TASK_SALONS, [])
    assert d.action is Action.SPAWN
    assert "no candidates" in d.reason.lower()


def test_route_is_pure():
    cands = [_strong(), _strong(session_id="s2", match_score=0.75)]
    d1 = route(TASK_SALONS, cands, capacity_policy={})
    d2 = route(TASK_SALONS, cands, capacity_policy={})
    assert d1.to_dict() == d2.to_dict()


# --------------------------------------------------------------------------- #
# recency filter
# --------------------------------------------------------------------------- #
def test_recency_drops_stale():
    stale = _strong(last_seen=_iso_days_ago(120))
    d = route(TASK_SALONS, [stale], recency_filter={"days": 60})
    assert d.action is Action.SPAWN  # dropped -> no candidates -> spawn


def test_recency_override_keeps_stale():
    stale = _strong(last_seen=_iso_days_ago(120))
    d = route(TASK_SALONS, [stale],
              recency_filter={"days": 60, "override": True})
    assert d.action is Action.REUSE  # kept despite being stale


# --------------------------------------------------------------------------- #
# four-criteria ranking
# --------------------------------------------------------------------------- #
def test_higher_coverage_and_scores_rank_first():
    central = _strong(session_id="central", salons=TASK_SALONS,
                      match_score=0.95, centrality=0.95)
    tangential = _strong(session_id="tangential", salons=["ptp/packaging"],
                         match_score=0.72, centrality=0.55)
    d = route(TASK_SALONS, [tangential, central])
    # best-first ranking: the central, fuller-coverage candidate wins.
    assert d.scored_candidates[0].session_id == "central"
    assert d.chosen_session_id == "central"


# --------------------------------------------------------------------------- #
# injection
# --------------------------------------------------------------------------- #
def test_assemble_payload_bounded_and_labeled():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "steering.md"
        f.write_text("rule one\nrule two", encoding="utf-8")
        payload = assemble_payload([str(f), {"label": "mp", "text": "cutting"}])
        assert "## SOURCE:" in payload and "cutting" in payload
        assert "rule one" in payload


def test_assemble_payload_missing_source_fails_loud():
    try:
        assemble_payload(["/nonexistent/path/xyz"])
        raise AssertionError("expected SourceNotFoundError")
    except SourceNotFoundError:
        pass


def test_assemble_payload_too_large_fails():
    os.environ["STEWARD_MAX_INJECTION_BYTES"] = "50"
    try:
        assemble_payload([{"label": "big", "text": "x" * 500}])
        raise AssertionError("expected InjectionTooLargeError")
    except InjectionTooLargeError:
        pass
    finally:
        os.environ.pop("STEWARD_MAX_INJECTION_BYTES", None)


def test_inject_knowledge_calls_deliver():
    delivered = {}

    def fake_deliver(tmux, prompt, require_ready=True):
        delivered["tmux"] = tmux
        delivered["bytes"] = len(prompt.encode("utf-8"))

    port, _ = _fake_port(deliver=fake_deliver)
    out = inject_knowledge("knight-1", [{"label": "c", "text": "hello"}],
                           port=port)
    assert delivered["tmux"] == "knight-1"
    assert out["bytes"] == delivered["bytes"] and out["blocks"] == 1


# --------------------------------------------------------------------------- #
# execute_decision -> right motor/journey call
# --------------------------------------------------------------------------- #
class _FakeKnight:
    alive = True
    tmux_session = None  # forces REUSE to rebuild a runtime via resume


def _fake_port(**over):
    calls: list = []
    roster: list = []  # what register_knight persisted (for get_journey readback)

    def spawn(agent, cwd=None):
        calls.append(("spawn", agent))
        return {"tmux_session": "t-spawn", "agent": agent,
                "session_id": "sess_new", "resolved": True}

    def resume(session_id, cwd=None):
        calls.append(("resume", session_id))
        return {"tmux_session": "t-resume", "session_id": session_id,
                "resolved": True}

    def deliver(tmux, prompt, require_ready=True):
        calls.append(("deliver", tmux))

    def read_session(session_id):
        calls.append(("read_session", session_id))
        return _FakeKnight()

    def scan_knights():
        calls.append(("scan_knights",))
        return {"knights": []}

    def register_knight(journey_id, role, agent, session_id, tmux_session,
                        resolved=None):
        calls.append(("register_knight", role, tmux_session))
        entry = {"role": role, "agent": agent, "session_id": session_id,
                 "tmux_session": tmux_session,
                 "resolved": bool(resolved if resolved is not None
                                  else session_id is not None)}
        roster[:] = [k for k in roster if k["role"] != role]
        roster.append(entry)
        return entry

    # spawn_knight default: resolves the id (simulates the common happy path).
    _spawn_resolved = over.get("spawn_resolved", True)
    _spawn_session_id = over.get("spawn_session_id", "sess_new")

    def spawn_knight(journey_id, role, agent, cwd=None):
        calls.append(("spawn_knight", role, agent))
        register_knight(journey_id, role, agent,
                        _spawn_session_id if _spawn_resolved else None,
                        "t-young-recruit", resolved=_spawn_resolved)
        return "t-young-recruit"

    def get_journey(journey_id):
        calls.append(("get_journey", journey_id))
        return {"journey_id": journey_id, "knights": list(roster)}

    port = StewardPort(
        spawn=over.get("spawn", spawn),
        resume=over.get("resume", resume),
        deliver=over.get("deliver", deliver),
        read_session=over.get("read_session", read_session),
        scan_knights=over.get("scan_knights", scan_knights),
        register_knight=over.get("register_knight", register_knight),
        spawn_knight=over.get("spawn_knight", spawn_knight),
        get_journey=over.get("get_journey", get_journey),
    )
    return port, calls


def _mk_decision(action, sid=None):
    from steward.routing import RoutingDecision
    return RoutingDecision(action, sid, "test", tuple())


def test_execute_reuse_rebuilds_runtime_and_registers():
    port, calls = _fake_port()
    out = execute_decision("j1", "coder",
                           _mk_decision(Action.REUSE, "sess_x"), "wrcp",
                           port=port)
    kinds = [c[0] for c in calls]
    assert out["action"] == "REUSE"
    assert "read_session" in kinds and "resume" in kinds
    assert "register_knight" in kinds


def test_execute_resume_calls_resume():
    port, calls = _fake_port()
    out = execute_decision("j1", "coder",
                           _mk_decision(Action.RESUME, "sess_dead"), "wrcp",
                           port=port)
    assert ("resume", "sess_dead") in calls
    assert out["tmux_session"] == "t-resume"


def test_execute_spawn_calls_spawn_knight():
    port, calls = _fake_port()
    out = execute_decision("j1", "coder", _mk_decision(Action.SPAWN), "wrcp",
                           port=port)
    assert any(c[0] == "spawn_knight" for c in calls)
    assert out["tmux_session"] == "t-young-recruit"


def test_execute_extract_fresh_spawns_and_injects():
    port, calls = _fake_port()
    out = execute_decision("j1", "coder",
                           _mk_decision(Action.EXTRACT_FRESH, "sess_full"),
                           "wrcp", port=port,
                           extract_summary="prior: did ptp packaging")
    kinds = [c[0] for c in calls]
    assert out["action"] == "EXTRACT_FRESH"
    assert "spawn_knight" in kinds and "deliver" in kinds  # injected summary


def test_execute_spawn_with_injection():
    port, calls = _fake_port()
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "agent.md"
        f.write_text("WRA knowledge", encoding="utf-8")
        execute_decision("j1", "coder", _mk_decision(Action.SPAWN), "wrcp",
                         port=port, inject_sources=[str(f)])
    kinds = [c[0] for c in calls]
    assert "spawn_knight" in kinds and "deliver" in kinds


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def test_keep_is_noop_record():
    assert keep("sess_x") == {"session_id": "sess_x", "lifecycle": "keep"}


def test_retire_marks_index():
    index = {"knights": [{"session_id": "sess_x", "essence": "ptp"}]}
    out = retire("sess_x", index)
    row = out["knights"][0]
    assert row["retired"] is True and "retired_at" in row
    assert row["essence"] == "ptp"  # essence never hand-edited


def test_retire_missing_raises():
    try:
        retire("nope", {"knights": []})
        raise AssertionError("expected StewardError")
    except StewardError:
        pass


class _AlivePort:
    def read_session(self, session_id):
        class K:
            alive = True
        return K()


class _DeadPort:
    def read_session(self, session_id):
        class K:
            alive = False
        return K()


def test_delete_refused_without_confirm():
    try:
        delete("sess_x", confirm=False, port=_DeadPort())
        raise AssertionError("expected DeleteRefusedError")
    except DeleteRefusedError:
        pass


def test_delete_refused_when_alive_without_force():
    try:
        delete("sess_x", confirm=True, force=False, port=_AlivePort())
        raise AssertionError("expected DeleteRefusedError")
    except DeleteRefusedError:
        pass


def test_delete_proceeds_when_dead_and_confirmed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "cli").mkdir()
        (root / "cli" / "sess_x.json").write_text("{}", encoding="utf-8")
        out = delete("sess_x", confirm=True, port=_DeadPort(), root=root)
        assert out["deleted"] is True
        assert not (root / "cli" / "sess_x.json").exists()


def test_delete_proceeds_when_alive_but_forced():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        wsdir = root / "hash1" / "sess_live"
        wsdir.mkdir(parents=True)
        (wsdir / "session.json").write_text("{}", encoding="utf-8")
        out = delete("sess_live", confirm=True, force=True, port=_AlivePort(),
                     root=root)
        assert out["deleted"] is True
        assert not wsdir.exists()


# --------------------------------------------------------------------------- #
# C3 (MAJOR) — capacity-unknown (context_pct=None) routing
# --------------------------------------------------------------------------- #
def test_route_capacity_unknown_reuses_with_explicit_reason():
    cand = _strong(context_pct=None)  # v3 pre-first-turn / unknown-window agent
    d = route(TASK_SALONS, [cand])
    assert d.action is Action.REUSE
    assert d.chosen_session_id == "sess_strong"
    assert "unknown" in d.reason.lower()   # must NOT claim a measured 0%
    assert "0%" not in d.reason
    # scored candidate exposes headroom_pct=None (not 0)
    assert d.scored_candidates[0].to_dict()["headroom_pct"] is None


def test_route_capacity_unknown_dead_still_resumes():
    cand = _strong(context_pct=None, alive=False)
    d = route(TASK_SALONS, [cand])
    assert d.action is Action.RESUME  # dead takes precedence over unknown cap


# --------------------------------------------------------------------------- #
# C5 (MAJOR) — execute_decision surfaces the motor resolved/session_id contract
# --------------------------------------------------------------------------- #
def test_execute_spawn_surfaces_unresolved():
    # spawn_knight could NOT resolve the durable id -> executor must surface it.
    port, calls = _fake_port(spawn_resolved=False)
    out = execute_decision("j1", "coder", _mk_decision(Action.SPAWN), "wrcp",
                           port=port)
    assert out["resolved"] is False
    assert out["session_id"] is None


def test_execute_spawn_surfaces_resolved():
    port, calls = _fake_port(spawn_resolved=True, spawn_session_id="sess_ok")
    out = execute_decision("j1", "coder", _mk_decision(Action.SPAWN), "wrcp",
                           port=port)
    assert out["resolved"] is True
    assert out["session_id"] == "sess_ok"


def test_execute_extract_fresh_surfaces_resolution():
    port, calls = _fake_port(spawn_resolved=False)
    out = execute_decision("j1", "coder",
                           _mk_decision(Action.EXTRACT_FRESH, "sess_full"),
                           "wrcp", port=port, extract_summary="prior work")
    assert out["resolved"] is False and out["session_id"] is None


def test_execute_resume_honors_port_resolved():
    # A resume that reports resolved:false must NOT be hardcoded to True.
    def resume_unresolved(session_id, cwd=None):
        return {"tmux_session": "t-r", "session_id": None, "resolved": False}
    port, calls = _fake_port(resume=resume_unresolved)
    out = execute_decision("j1", "coder",
                           _mk_decision(Action.RESUME, "sess_x"), "wrcp",
                           port=port)
    assert out["resolved"] is False


# --------------------------------------------------------------------------- #
# C1 (BLOCKER) — delete() path-traversal refusal
# --------------------------------------------------------------------------- #
def _sandbox_with_siblings():
    """Create a sessions root with a sibling/parent file to prove none touched."""
    tmp = tempfile.mkdtemp()
    base = Path(tmp) / "sessions"
    (base / "cli").mkdir(parents=True)
    (base / "cli" / "real.json").write_text("REAL", encoding="utf-8")
    # a file ABOVE cli/ (inside base) and a file OUTSIDE base entirely
    (base / "secret_top.json").write_text("SECRET", encoding="utf-8")
    (Path(tmp) / "outside.json").write_text("OUTSIDE", encoding="utf-8")
    return tmp, base


class _DeadPortLocal:
    def read_session(self, session_id):
        class K:
            alive = False
            source_path = None
        return K()


def test_delete_rejects_traversal_ids():
    import shutil as _sh
    for bad in ["../secret_top", "../cli/real", "/etc/passwd", "a/b", "..",
                "", "."]:
        tmp, base = _sandbox_with_siblings()
        try:
            raised = False
            try:
                delete(bad, confirm=True, force=True, port=_DeadPortLocal(),
                       root=base)
            except StewardError:
                raised = True
            assert raised, f"delete({bad!r}) must raise StewardError"
            # nothing was touched
            assert (base / "cli" / "real.json").exists()
            assert (base / "secret_top.json").exists()
            assert (Path(tmp) / "outside.json").exists()
        finally:
            _sh.rmtree(tmp, ignore_errors=True)


def test_delete_wellformed_id_still_works():
    import shutil as _sh
    tmp, base = _sandbox_with_siblings()
    try:
        # a legit v1/v2 id (no separators) deletes only its own group
        (base / "cli" / "legit.json").write_text("X", encoding="utf-8")
        out = delete("legit", confirm=True, force=True, port=_DeadPortLocal(),
                     root=base)
        assert out["deleted"] is True
        assert not (base / "cli" / "legit.json").exists()
        # siblings untouched
        assert (base / "cli" / "real.json").exists()
        assert (base / "secret_top.json").exists()
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def test_delete_prefers_adapter_source_path():
    import shutil as _sh
    tmp, base = _sandbox_with_siblings()
    try:
        # source_path points at the exact v1/v2 json the adapter resolved.
        target = base / "cli" / "viasrc.json"
        target.write_text("X", encoding="utf-8")

        class _PortWithSource:
            def read_session(self, session_id):
                class K:
                    alive = False
                    source_path = str(target)
                return K()

        out = delete("viasrc", confirm=True, force=True, port=_PortWithSource(),
                     root=base)
        assert out["deleted"] is True
        assert not target.exists()
        assert (base / "cli" / "real.json").exists()
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# C1 (MINOR) — delete fails CLOSED on an unreadable (corrupt) session
# --------------------------------------------------------------------------- #
def test_delete_fails_closed_on_corrupt_read():
    from motor import SessionCorruptError

    class _CorruptPort:
        def read_session(self, session_id):
            raise SessionCorruptError("bad bytes")

    try:
        delete("sess_x", confirm=True, force=False, port=_CorruptPort())
        raise AssertionError("expected DeleteRefusedError (fail closed)")
    except DeleteRefusedError:
        pass


def test_delete_absent_session_is_safe_without_force():
    import shutil as _sh
    from motor import SessionNotFoundError

    class _AbsentPort:
        def read_session(self, session_id):
            raise SessionNotFoundError("gone")

    tmp, base = _sandbox_with_siblings()
    try:
        # truly-absent -> safe to reap without force (removes nothing here)
        out = delete("nonexistent", confirm=True, force=False,
                     port=_AbsentPort(), root=base)
        assert out["deleted"] is True
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# recency date-parsing edge cases (MINOR gap)
# --------------------------------------------------------------------------- #
def test_recency_malformed_last_seen_is_kept():
    cand = _strong(last_seen="not-a-date")
    d = route(TASK_SALONS, [cand], recency_filter={"days": 30})
    assert d.action is Action.REUSE  # unparseable -> cannot prove stale -> kept


def test_recency_absent_last_seen_is_kept():
    cand = _strong()
    del cand["last_seen"]
    d = route(TASK_SALONS, [cand], recency_filter={"days": 30})
    assert d.action is Action.REUSE


def test_recency_naive_timestamp_is_handled():
    # tz-less (naive) recent timestamp -> treated as UTC -> kept
    naive = (datetime.now(timezone.utc)).replace(tzinfo=None).isoformat()
    cand = _strong(last_seen=naive)
    d = route(TASK_SALONS, [cand], recency_filter={"days": 30})
    assert d.action is Action.REUSE


# --------------------------------------------------------------------------- #
# bare runner
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
