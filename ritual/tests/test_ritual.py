"""Deterministic, isolated unit tests for Ritual A (Phase 4).

100% isolated: NO real tmux/kiro. The lower layers are driven through a fake
:class:`RitualPort`; a real journey meta.json is used (tmpdir) only to prove the
synthesis is NOT persisted (anti-verbosity).

Run:  python3 ritual/tests/test_ritual.py
  or: python3 -m pytest ritual/tests -q
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ritual import (  # noqa: E402
    ArchmaesterSynthesis,
    DepositForbiddenError,
    RitualError,
    RitualPort,
    build_investigation_prompt,
    ritual_a,
)


# --------------------------------------------------------------------------- #
# fake port
# --------------------------------------------------------------------------- #
def _fake_port(**over):
    calls: list = []
    roster: list = []

    def spawn_knight(journey_id, role, agent, cwd=None):
        calls.append(("spawn_knight", role, agent))
        roster.append({"role": role, "agent": agent,
                       "session_id": "sess_arch", "tmux_session": "t-arch",
                       "resolved": True})
        return "t-arch"

    def command_knight(journey_id, role_or_tmux, prompt):
        calls.append(("command_knight", role_or_tmux, prompt))
        return ("ARCHIMEDEAN POINT: X is well-understood.\n"
                "RISKS: none major.\nUNKNOWNS: edge cases.")

    def get_journey(journey_id):
        calls.append(("get_journey", journey_id))
        return {"journey_id": journey_id, "knights": list(roster)}

    def route(task_salons, candidates, *a, **k):
        calls.append(("route", tuple(task_salons)))
        from steward.routing import RoutingDecision, Action
        return RoutingDecision(Action.REUSE, "sess_existing_arch",
                               "reuse archmaester who studied X", tuple())

    def execute_decision(journey_id, role, decision, agent, *a, **k):
        calls.append(("execute_decision", decision.action.value))
        roster.append({"role": role, "agent": agent,
                       "session_id": "sess_existing_arch",
                       "tmux_session": "t-reused", "resolved": True})
        return {"action": decision.action.value, "tmux_session": "t-reused",
                "session_id": "sess_existing_arch", "resolved": True,
                "role": role}

    port = RitualPort(
        spawn_knight=over.get("spawn_knight", spawn_knight),
        command_knight=over.get("command_knight", command_knight),
        get_journey=over.get("get_journey", get_journey),
        route=over.get("route", route),
        execute_decision=over.get("execute_decision", execute_decision),
    )
    return port, calls


# --------------------------------------------------------------------------- #
# ritual_a — spawn-fresh path
# --------------------------------------------------------------------------- #
def test_ritual_a_spawns_fresh_when_no_candidates():
    port, calls = _fake_port()
    out = ritual_a("j1", "PTP holdover behavior",
                   refs=[{"type": "text", "value": "holdover spec"}],
                   port=port)
    assert isinstance(out, ArchmaesterSynthesis)
    assert out.delivered is True and out.deposited is False
    assert out.archmaester_session_id == "sess_arch"
    assert "ARCHIMEDEAN POINT" in out.synthesis_text
    kinds = [c[0] for c in calls]
    assert "spawn_knight" in kinds and "command_knight" in kinds
    # spawned fresh -> did NOT route
    assert "route" not in kinds


def test_ritual_a_commands_with_investigation_prompt():
    port, calls = _fake_port()
    ritual_a("j1", "ceph rebalancing",
             refs=[{"type": "jira", "value": "CGTS-123"}], port=port)
    cmd = next(c for c in calls if c[0] == "command_knight")
    prompt = cmd[2]
    assert "ceph rebalancing" in prompt
    assert "CGTS-123" in prompt
    assert "DELIVER" in prompt and "deposit" in prompt.lower()


# --------------------------------------------------------------------------- #
# ritual_a — reuse-by-essence path (candidates supplied)
# --------------------------------------------------------------------------- #
def test_ritual_a_routes_when_candidates_supplied():
    port, calls = _fake_port()
    candidates = [{"session_id": "sess_existing_arch", "agent": "wrcp",
                   "salons": ["ptp/packaging"], "context_pct": 20.0,
                   "window": 1_000_000, "alive": True, "last_seen": None,
                   "match_score": 0.9, "centrality": 0.9}]
    out = ritual_a("j1", "ptp packaging",
                   refs=[{"type": "text", "value": "x"}],
                   port=port, candidates=candidates,
                   task_salons=["ptp/packaging"])
    kinds = [c[0] for c in calls]
    # routed + executed instead of a bare spawn_knight
    assert "route" in kinds and "execute_decision" in kinds
    assert ("spawn_knight", "archmaester", "wrcp") not in calls
    assert out.archmaester_session_id == "sess_existing_arch"
    assert out.deposited is False


# --------------------------------------------------------------------------- #
# build_investigation_prompt
# --------------------------------------------------------------------------- #
def test_build_prompt_contains_all_required_elements():
    refs = [
        {"type": "jira", "value": "CGTS-99"},
        {"type": "confluence", "value": "/wiki/ptp"},
        {"type": "gerrit", "value": "12345"},
        {"type": "url", "value": "https://example.com"},
        {"type": "file", "value": "/repo/spec.md"},
        {"type": "text", "value": "free text note"},
    ]
    p = build_investigation_prompt("PTP subject", refs)
    assert "PTP subject" in p
    for ref in refs:
        assert str(ref["value"]) in p
    assert "MemPalace" in p and "FIRST" in p
    assert "ARCHIMEDEAN POINT" in p
    assert "RISKS" in p and "UNKNOWNS" in p
    assert "DELIVER" in p and "do NOT deposit" in p


def test_build_prompt_empty_subject_raises():
    try:
        build_investigation_prompt("  ", [])
        raise AssertionError("expected RitualError")
    except RitualError:
        pass


def test_build_prompt_unknown_ref_type_raises():
    try:
        build_investigation_prompt("s", [{"type": "smoke-signal", "value": "x"}])
        raise AssertionError("expected RitualError")
    except RitualError:
        pass


def test_build_prompt_empty_ref_value_raises():
    try:
        build_investigation_prompt("s", [{"type": "jira", "value": ""}])
        raise AssertionError("expected RitualError")
    except RitualError:
        pass


# --------------------------------------------------------------------------- #
# anti-verbosity: synthesis NOT written into meta.json
# --------------------------------------------------------------------------- #
def test_synthesis_not_persisted_to_meta():
    import journey
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = tmp
        try:
            jid = journey.journey_create("investigate ptp")
            # Fake spawn/command but use the REAL journey register + meta so we
            # can assert the synthesis text never lands in meta.json.
            def spawn_knight(journey_id, role, agent, cwd=None):
                journey.register_knight(journey_id, role=role, agent=agent,
                                        session_id="sess_arch",
                                        tmux_session="t-arch", resolved=True)
                return "t-arch"

            def command_knight(journey_id, role_or_tmux, prompt):
                return "SECRET_SYNTHESIS_MARKER: the archimedean point is Y"

            port, _ = _fake_port(spawn_knight=spawn_knight,
                                 command_knight=command_knight,
                                 get_journey=journey.get_journey)
            out = ritual_a(jid, "ptp", refs=[{"type": "text", "value": "z"}],
                           port=port)
            assert "SECRET_SYNTHESIS_MARKER" in out.synthesis_text

            from journey.paths import meta_path
            meta_text = meta_path(jid).read_text()
            assert "SECRET_SYNTHESIS_MARKER" not in meta_text
            # roster is pointers-only
            for k in journey.get_journey(jid)["knights"]:
                assert set(k) <= {"role", "agent", "session_id",
                                  "tmux_session", "resolved"}
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev


# --------------------------------------------------------------------------- #
# delivers-never-deposits guard
# --------------------------------------------------------------------------- #
def test_ritual_a_never_triggers_a_deposit():
    port, calls = _fake_port()
    out = ritual_a("j1", "subj", refs=[{"type": "text", "value": "t"}],
                   port=port)
    # No call name resembles a deposit/mine.
    forbidden = {"mine", "deposit", "kg_add", "kg_supersede"}
    assert not any(c[0] in forbidden for c in calls)
    assert out.deposited is False


def test_ritual_port_rejects_deposit_capability():
    # A RitualPort subclass that exposes a deposit capability must be rejected.
    import ritual.ritual as rr

    class _BadPort(RitualPort):
        def mine(self, *a, **k):  # smuggled deposit capability
            return "deposited!"

    def spawn_knight(*a, **k):
        return "t"

    def noop(*a, **k):
        return {}

    try:
        _BadPort(spawn_knight=spawn_knight, command_knight=noop,
                 get_journey=noop, route=noop, execute_decision=noop)
        raise AssertionError("expected DepositForbiddenError")
    except DepositForbiddenError:
        pass


def test_synthesis_deposited_is_always_false():
    s = ArchmaesterSynthesis(subject="x", archmaester_session_id="s",
                             synthesis_text="t")
    assert s.deposited is False and s.delivered is True


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #
def test_ritual_a_empty_subject_raises():
    port, _ = _fake_port()
    try:
        ritual_a("j1", "", refs=[], port=port)
        raise AssertionError("expected RitualError")
    except RitualError:
        pass


def test_ritual_a_empty_journey_raises():
    port, _ = _fake_port()
    try:
        ritual_a("", "subj", refs=[], port=port)
        raise AssertionError("expected RitualError")
    except RitualError:
        pass


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
