"""Deterministic, isolated unit tests for the journey layer (Phase 2).

Same style as motor/tests/test_motor.py: 100% isolated. NO real tmux/kiro —
every motor call is injected via a fake :class:`MotorPort`. Each test runs in a
tmpdir set as the journeys root (``TMUX_MAINBRAIN_JOURNEYS``).

Run:  python3 journey/tests/test_journey.py
  or: python3 -m pytest journey/tests -q
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from journey import (  # noqa: E402
    MotorPort,
    command_knight,
    get_journey,
    journey_create,
    recover_knight,
    register_knight,
    resolve_knight,
    spawn_knight,
)
from journey.errors import AntiVerbosityError, UnresolvedKnightError  # noqa: E402
from journey.meta import validate_document  # noqa: E402
from journey.paths import meta_path  # noqa: E402


# --------------------------------------------------------------------------- #
# fakes + fixtures
# --------------------------------------------------------------------------- #
class _FakeMotor:
    """Records calls and returns scripted results. No real tmux/kiro."""

    def __init__(self, *, spawn_result=None, resume_result=None,
                 watch_result=None, peek_sequence=None, scan_result=None):
        self.calls = []
        self._spawn_result = spawn_result or {
            "tmux_session": "knight-x", "agent": "wrcp",
            "session_id": "sess_abc", "resolved": True}
        self._resume_result = resume_result or {
            "tmux_session": "knight-x-2", "session_id": "sess_abc",
            "resolved": True}
        self._watch_result = watch_result or {
            "done": True, "reason": "idle-transition", "elapsed_seconds": 3.0}
        self._peek_sequence = list(peek_sequence or ["baseline", "the answer"])
        self._peek_i = 0
        self._scan_result = scan_result or {"knights": []}

    def spawn(self, agent, cwd=None):
        self.calls.append(("spawn", agent))
        return dict(self._spawn_result)

    def resume(self, session_id, cwd=None):
        self.calls.append(("resume", session_id))
        return dict(self._resume_result)

    def deliver(self, tmux, prompt, require_ready=True):
        self.calls.append(("deliver", tmux, prompt))

    def watch(self, tmux, timeout=600):
        self.calls.append(("watch", tmux, timeout))
        return dict(self._watch_result)

    def peek(self, tmux, lines=200):
        val = self._peek_sequence[min(self._peek_i, len(self._peek_sequence) - 1)]
        self._peek_i += 1
        self.calls.append(("peek", tmux))
        return val

    def scan_knights(self):
        self.calls.append(("scan_knights",))
        return dict(self._scan_result)

    def port(self):
        return MotorPort(spawn=self.spawn, resume=self.resume,
                         deliver=self.deliver, watch=self.watch,
                         peek=self.peek, scan_knights=self.scan_knights)


def _isolated(fn):
    """Run ``fn(tmpdir)`` with TMUX_MAINBRAIN_JOURNEYS pointed at a tmpdir."""
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = tmp
        try:
            fn(Path(tmp))
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_journey_create_writes_exact_schema():
    def body(_tmp):
        jid = journey_create("build the young recruit loop", king_session="sess_king")
        doc = get_journey(jid)
        assert set(doc) == {"journey_id", "goal", "king_session", "status",
                            "created_at", "updated_at", "knights"}
        assert doc["goal"] == "build the young recruit loop"
        assert doc["king_session"] == "sess_king"
        assert doc["status"] == "active"
        assert doc["knights"] == []
        # on-disk file matches (validated)
        on_disk = json.loads(meta_path(jid).read_text())
        validate_document(on_disk)
    _isolated(body)


def test_anti_verbosity_rejects_multiline_goal():
    def body(_tmp):
        try:
            journey_create("line one\nline two")
            raise AssertionError("expected AntiVerbosityError")
        except AntiVerbosityError:
            pass
    _isolated(body)


def test_anti_verbosity_rejects_forbidden_knight_field():
    def body(_tmp):
        jid = journey_create("g")
        # A forbidden field (a transcript) must be rejected by the guard.
        from journey.meta import validate_knight_entry
        try:
            validate_knight_entry({
                "role": "coder", "agent": "wrcp", "session_id": "s",
                "tmux_session": "t", "transcript": "the whole convo"})
            raise AssertionError("expected AntiVerbosityError")
        except AntiVerbosityError:
            pass
    _isolated(body)


def test_anti_verbosity_rejects_multiline_knight_value():
    def body(_tmp):
        from journey.meta import validate_knight_entry
        try:
            validate_knight_entry({
                "role": "coder", "agent": "wrcp", "session_id": "s",
                "tmux_session": "line1\nline2"})
            raise AssertionError("expected AntiVerbosityError")
        except AntiVerbosityError:
            pass
    _isolated(body)


def test_register_unresolved_then_resolve():
    def body(_tmp):
        jid = journey_create("g")
        # spawn returned resolved:false, session_id None (motor contract).
        register_knight(jid, role="coder", agent="wrcp",
                        session_id=None, tmux_session="knight-coder-1",
                        resolved=False)
        doc = get_journey(jid)
        k = doc["knights"][0]
        assert k["tmux_session"] == "knight-coder-1"
        assert k["session_id"] is None and k["resolved"] is False

        # scan finds exactly one wrcp session -> resolve fills it.
        fake = _FakeMotor(scan_result={"knights": [
            {"agent": "wrcp", "session_id": "sess_resolved"}]})
        out = resolve_knight(jid, "coder", port=fake.port())
        assert out["session_id"] == "sess_resolved" and out["resolved"] is True
        # persisted
        assert get_journey(jid)["knights"][0]["session_id"] == "sess_resolved"
    _isolated(body)


def test_resolve_ambiguous_stays_unresolved():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id=None, tmux_session="t", resolved=False)
        fake = _FakeMotor(scan_result={"knights": [
            {"agent": "wrcp", "session_id": "s1"},
            {"agent": "wrcp", "session_id": "s2"}]})  # 2 candidates -> ambiguous
        out = resolve_knight(jid, "coder", port=fake.port())
        assert out["resolved"] is False and out["session_id"] is None
    _isolated(body)


def test_spawn_knight_registers_and_returns_tmux():
    def body(_tmp):
        jid = journey_create("g")
        fake = _FakeMotor(spawn_result={
            "tmux_session": "knight-coder-9", "agent": "wrcp",
            "session_id": "sess_1", "resolved": True})
        tmux = spawn_knight(jid, "coder", "wrcp", port=fake.port())
        assert tmux == "knight-coder-9"
        k = get_journey(jid)["knights"][0]
        assert k["session_id"] == "sess_1" and k["resolved"] is True
        assert ("spawn", "wrcp") in fake.calls
    _isolated(body)


def test_spawn_knight_unresolved_id_stored():
    def body(_tmp):
        jid = journey_create("g")
        fake = _FakeMotor(spawn_result={
            "tmux_session": "knight-coder-9", "agent": "wrcp",
            "session_id": None, "resolved": False})  # motor couldn't resolve
        spawn_knight(jid, "coder", "wrcp", port=fake.port())
        k = get_journey(jid)["knights"][0]
        assert k["tmux_session"] == "knight-coder-9"
        assert k["session_id"] is None and k["resolved"] is False
    _isolated(body)


def test_command_knight_returns_result_and_does_not_persist_it():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id="s", tmux_session="knight-coder-1")
        fake = _FakeMotor(peek_sequence=["prompt echo baseline",
                                         "prompt echo baseline\n5+5 = 10"])
        out = command_knight(jid, "coder", "what is 5+5?", port=fake.port())
        assert "10" in out
        # the result must NOT be anywhere in meta.json (anti-verbosity)
        meta_text = meta_path(jid).read_text()
        assert "5+5 = 10" not in meta_text
        assert "10" not in meta_text  # no transcript leaked
        # deliver + watch were called
        assert any(c[0] == "deliver" for c in fake.calls)
        assert any(c[0] == "watch" for c in fake.calls)
    _isolated(body)


def test_command_knight_slow_first_token_triggers_rewatch():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id="s", tmux_session="knight-coder-1")
        # peek sequence: [baseline, SAME-as-baseline (watch false-completed),
        #                 real answer after the re-watch]
        fake = _FakeMotor(
            watch_result={"done": True, "reason": "idle-stable"},
            peek_sequence=["BASE", "BASE", "BASE\nreal answer 42"])
        out = command_knight(jid, "coder", "compute", port=fake.port())
        assert "42" in out
        # watch was called TWICE (initial + one bounded re-watch)
        watch_calls = [c for c in fake.calls if c[0] == "watch"]
        assert len(watch_calls) == 2
    _isolated(body)


def test_command_knight_no_rewatch_when_output_changed():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id="s", tmux_session="knight-coder-1")
        fake = _FakeMotor(peek_sequence=["BASE", "BASE\nchanged answer"])
        command_knight(jid, "coder", "go", port=fake.port())
        watch_calls = [c for c in fake.calls if c[0] == "watch"]
        assert len(watch_calls) == 1  # output changed -> no re-watch
    _isolated(body)


def test_recover_knight_resumes_and_updates_tmux():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id="sess_durable", tmux_session="old-tmux",
                        resolved=True)
        fake = _FakeMotor(resume_result={
            "tmux_session": "new-tmux", "session_id": "sess_durable",
            "resolved": True})
        new_tmux = recover_knight(jid, "coder", port=fake.port())
        assert new_tmux == "new-tmux"
        assert get_journey(jid)["knights"][0]["tmux_session"] == "new-tmux"
        # durable session_id unchanged
        assert get_journey(jid)["knights"][0]["session_id"] == "sess_durable"
        assert ("resume", "sess_durable") in fake.calls
    _isolated(body)


def test_recover_unresolved_knight_raises():
    def body(_tmp):
        jid = journey_create("g")
        register_knight(jid, role="coder", agent="wrcp",
                        session_id=None, tmux_session="t", resolved=False)
        fake = _FakeMotor()
        try:
            recover_knight(jid, "coder", port=fake.port())
            raise AssertionError("expected UnresolvedKnightError")
        except UnresolvedKnightError:
            pass
    _isolated(body)


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
