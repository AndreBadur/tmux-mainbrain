"""Deterministic, isolated unit tests for Ritual B curation (Phase 5).

100% isolated: NO real tmux/kiro, NO live spawn. motor.mine + journey + steward
lifecycle are driven through a fake :class:`CurationPort`. A real journey
meta.json (tmpdir) is used only to prove reports are NOT persisted.

Run:  python3 curation/tests/test_curation.py
  or: python3 -m pytest curation/tests -q
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from curation import (  # noqa: E402
    CurationPort,
    DepositResult,
    KnightReport,
    ProvenanceError,
    RitualBResult,
    SaberDrawer,
    arbitrate_lifecycle,
    collect_spoils,
    curate_saber,
    deposit_saber,
    drawer_from_dict,
    ritual_b,
)
from curation.errors import CurationError  # noqa: E402
from steward.errors import DeleteRefusedError  # noqa: E402


# --------------------------------------------------------------------------- #
# fake port
# --------------------------------------------------------------------------- #
def _fake_port(**over):
    calls: list = []
    roster = over.get("roster", [
        {"role": "coder", "agent": "wrcp", "session_id": "sess_c",
         "tmux_session": "t-c", "resolved": True},
        {"role": "reviewer", "agent": "wrcp", "session_id": "sess_r",
         "tmux_session": "t-r", "resolved": True},
    ])

    def command_knight(journey_id, role_or_tmux, prompt):
        calls.append(("command_knight", role_or_tmux))
        return f"WORKED: {role_or_tmux} did X\nFAILED: {role_or_tmux} missed Y"

    def get_journey(journey_id):
        calls.append(("get_journey", journey_id))
        return {"journey_id": journey_id, "knights": list(roster)}

    def mine(session_jsonl, wing, room, agent="motor", dry_run=False):
        calls.append(("mine", wing, room, agent))
        return {"wing": wing, "room": room, "mined": True}

    def keep(session_id):
        calls.append(("keep", session_id))
        return {"session_id": session_id, "lifecycle": "keep"}

    def retire(session_id, index):
        calls.append(("retire", session_id))
        return index

    def delete(session_id, force=False, confirm=False):
        calls.append(("delete", session_id, force, confirm))
        if not confirm:
            raise DeleteRefusedError("confirm required")
        return {"session_id": session_id, "deleted": True}

    def scan_knights():
        calls.append(("scan_knights",))
        return {"knights": []}

    port = CurationPort(
        command_knight=over.get("command_knight", command_knight),
        get_journey=over.get("get_journey", get_journey),
        mine=over.get("mine", mine),
        keep=over.get("keep", keep),
        retire=over.get("retire", retire),
        delete=over.get("delete", delete),
        scan_knights=over.get("scan_knights", scan_knights),
    )
    return port, calls


def _good_drawers():
    return [
        {"wing": "ptp", "room": "packaging", "content": "PTP fact A held",
         "validated": True, "provenance_note": "coder confirmed"},
        {"wing": "ptp", "room": "research", "content": "hypothesis B untested",
         "validated": False, "provenance_note": "archmaester research"},
    ]


# --------------------------------------------------------------------------- #
# collect_spoils
# --------------------------------------------------------------------------- #
def test_collect_spoils_commands_each_knight():
    port, calls = _fake_port()
    reports = collect_spoils("j1", port=port)
    assert len(reports) == 2
    assert all(isinstance(r, KnightReport) for r in reports)
    assert reports[0].worked and reports[0].failed
    cmd_calls = [c for c in calls if c[0] == "command_knight"]
    assert len(cmd_calls) == 2  # one per knight


def test_collect_spoils_reports_not_persisted():
    import journey
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = tmp
        try:
            jid = journey.journey_create("close journey")
            journey.register_knight(jid, role="coder", agent="wrcp",
                                    session_id="sess_c", tmux_session="t-c",
                                    resolved=True)

            def command_knight(journey_id, role, prompt):
                return "WORKED: SECRET_REPORT_MARKER\nFAILED: none"

            port, _ = _fake_port(command_knight=command_knight,
                                 get_journey=journey.get_journey)
            reports = collect_spoils(jid, port=port)
            assert "SECRET_REPORT_MARKER" in reports[0].worked
            from journey.paths import meta_path
            assert "SECRET_REPORT_MARKER" not in meta_path(jid).read_text()
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev


# --------------------------------------------------------------------------- #
# curate_saber — provenance mandatory
# --------------------------------------------------------------------------- #
def test_curate_saber_accepts_wellformed():
    drawers = curate_saber("ptp", None, [], _good_drawers())
    assert len(drawers) == 2
    assert drawers[0].validated is True and drawers[1].validated is False


def test_curate_saber_refuses_drawer_missing_validated():
    bad = [{"wing": "ptp", "room": "packaging", "content": "no provenance"}]
    try:
        curate_saber("ptp", None, [], bad)
        raise AssertionError("expected ProvenanceError")
    except ProvenanceError:
        pass


def test_curate_saber_refuses_non_bool_validated():
    bad = [{"wing": "w", "room": "r", "content": "c", "validated": "yes"}]
    try:
        curate_saber("s", None, [], bad)
        raise AssertionError("expected ProvenanceError")
    except ProvenanceError:
        pass


def test_drawer_from_dict_requires_content():
    try:
        drawer_from_dict({"wing": "w", "room": "r", "validated": True})
        raise AssertionError("expected ProvenanceError")
    except ProvenanceError:
        pass


# --------------------------------------------------------------------------- #
# deposit_saber — dry_run vs real; provenance recorded
# --------------------------------------------------------------------------- #
def test_deposit_dry_run_records_without_mining():
    port, calls = _fake_port()
    drawers = curate_saber("ptp", None, [], _good_drawers())
    result = deposit_saber(drawers, port=port, dry_run=True)
    assert isinstance(result, DepositResult) and result.dry_run is True
    assert len(result.deposited) == 2
    assert result.deposited[0]["wing"] == "ptp"
    assert result.deposited[0]["validated"] is True
    assert result.deposited[1]["validated"] is False
    # mine was NOT called
    assert not any(c[0] == "mine" for c in calls)


def test_deposit_real_calls_mine_per_drawer():
    port, calls = _fake_port()
    drawers = curate_saber("ptp", None, [], _good_drawers())
    result = deposit_saber(drawers, port=port, dry_run=False)
    mine_calls = [c for c in calls if c[0] == "mine"]
    assert len(mine_calls) == 2
    # right wing/room per drawer
    assert ("mine", "ptp", "packaging", "archmaester") in mine_calls
    assert ("mine", "ptp", "research", "archmaester") in mine_calls
    assert result.dry_run is False


def test_deposit_empty_is_noop():
    port, calls = _fake_port()
    result = deposit_saber([], port=port)
    assert result.deposited == ()
    assert not any(c[0] == "mine" for c in calls)


# --------------------------------------------------------------------------- #
# arbitrate_lifecycle — routes to steward ops; delete gate respected
# --------------------------------------------------------------------------- #
def test_arbitrate_routes_keep_retire_delete():
    port, calls = _fake_port()
    decisions = [
        {"session_id": "s1", "fate": "keep"},
        {"session_id": "s2", "fate": "retire"},
        {"session_id": "s3", "fate": "delete", "confirm": True, "force": True},
    ]
    out = arbitrate_lifecycle("j1", decisions, port=port,
                              index={"knights": []})
    kinds = [c[0] for c in calls]
    assert "keep" in kinds and "retire" in kinds and "delete" in kinds
    assert len(out.applied) == 3


def test_arbitrate_delete_without_confirm_refused():
    port, calls = _fake_port()
    decisions = [{"session_id": "s3", "fate": "delete"}]  # no confirm
    try:
        arbitrate_lifecycle("j1", decisions, port=port)
        raise AssertionError("expected DeleteRefusedError (gate respected)")
    except DeleteRefusedError:
        pass


def test_arbitrate_invalid_fate_raises():
    port, _ = _fake_port()
    try:
        arbitrate_lifecycle("j1", [{"session_id": "s", "fate": "banish"}],
                            port=port)
        raise AssertionError("expected CurationError")
    except CurationError:
        pass


# --------------------------------------------------------------------------- #
# ritual_b — full flow
# --------------------------------------------------------------------------- #
def test_ritual_b_end_to_end():
    port, calls = _fake_port()
    result = ritual_b(
        "j1", subject="ptp", synthesis=None,
        curated_drawers=_good_drawers(),
        lifecycle_decisions=[{"session_id": "sess_c", "fate": "keep"}],
        port=port)
    assert isinstance(result, RitualBResult)
    assert len(result.reports) == 2         # collected from 2 knights
    assert len(result.deposit.deposited) == 2
    assert len(result.lifecycle.applied) == 1
    kinds = [c[0] for c in calls]
    assert "command_knight" in kinds and "mine" in kinds and "keep" in kinds


def test_ritual_b_trivial_journey_deposits_nothing():
    port, calls = _fake_port()
    result = ritual_b(
        "j1", subject="trivial", synthesis=None,
        curated_drawers=[],          # nothing worthy to deposit
        lifecycle_decisions=[{"session_id": "sess_c", "fate": "keep"}],
        port=port)
    assert result.deposit.deposited == ()        # nothing deposited
    assert not any(c[0] == "mine" for c in calls)
    # but still collected + arbitrated (the closing step is guaranteed)
    assert len(result.reports) == 2
    assert len(result.lifecycle.applied) == 1


def test_ritual_b_dry_run_plans_deposit():
    port, calls = _fake_port()
    result = ritual_b(
        "j1", subject="ptp", synthesis=None,
        curated_drawers=_good_drawers(),
        lifecycle_decisions=[], port=port, dry_run=True)
    assert result.deposit.dry_run is True
    assert len(result.deposit.deposited) == 2
    assert not any(c[0] == "mine" for c in calls)  # dry-run: no real mine


# --------------------------------------------------------------------------- #
# the two-moments guard: no provenance -> refused BEFORE any deposit
# --------------------------------------------------------------------------- #
def test_two_moments_untagged_drawer_refused_before_deposit():
    port, calls = _fake_port()
    bad = [{"wing": "w", "room": "r", "content": "untested claim"}]  # no flag
    try:
        ritual_b("j1", "s", None, bad, [], port=port)
        raise AssertionError("expected ProvenanceError")
    except ProvenanceError:
        pass
    # crucially: NOTHING was mined/deposited before the refusal
    assert not any(c[0] == "mine" for c in calls)


def test_synthesis_deposited_false_is_the_ritual_a_counterpart():
    # A Ritual A synthesis is hypothesis (deposited:false); Ritual B is where a
    # validated:true drawer can finally be deposited. Sanity-check the contrast.
    from ritual import ArchmaesterSynthesis
    s = ArchmaesterSynthesis(subject="x", archmaester_session_id="a",
                             synthesis_text="hyp")
    assert s.deposited is False
    drawers = curate_saber("x", s, [], [
        {"wing": "w", "room": "r", "content": "proven in battle",
         "validated": True}])
    assert drawers[0].validated is True  # only NOW may it be deposited


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #
def test_collect_spoils_empty_journey_raises():
    port, _ = _fake_port()
    try:
        collect_spoils("", port=port)
        raise AssertionError("expected CurationError")
    except CurationError:
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
