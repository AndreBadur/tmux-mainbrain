"""Deterministic, isolated unit tests for the motor's pure logic.

Lens 5 (Contract Verification): 100% isolated — no real tmux, no kiro-cli, no
mempalace, no live sessions. Every test builds a synthetic sessions tree under
a tmpdir and drives the format adapter / scan / mining preprocessor.

Run:  python -m pytest motor/tests/test_motor.py -q
  or: python motor/tests/test_motor.py   (falls back to a bare runner)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from motor.errors import (  # noqa: E402
    DeliveryError,
    MiningError,
    SessionNotFoundError,
    TmuxTimeoutError,
    UnknownSessionFormatError,
)
from motor.format_adapter import read_session, read_session_path  # noqa: E402
from motor.knight_model import Liveness, SessionFormat  # noqa: E402
from motor.mining import _detect_and_clean, _render_convo  # noqa: E402
from motor.sessions import read_turns, scan_knights  # noqa: E402
from motor import tmux_driver  # noqa: E402
from motor.tmux_driver import (  # noqa: E402
    _resolve_new_session_id,
    watch,
)


# --------------------------------------------------------------------------- #
# fixtures (hand-built synthetic sessions — deterministic)
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_v1v2(root: Path, sid: str, *, state: bool, fresh: bool,
               with_lock: bool = True, fresh_jsonl: bool = False) -> None:
    """Create a v1/v2 session.

    state=False    => session_state null (fresh recruit).
    fresh          => the .lock mtime is recent (only when with_lock).
    with_lock      => write a .lock (set False to exercise the lockless path).
    fresh_jsonl    => touch the .jsonl to now (transcript-mtime liveness signal).
    """
    cli = root / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    if state:
        doc = {
            "session_id": sid, "cwd": "/work", "title": "v12 title",
            "updated_at": "2026-09-01T00:00:00Z",
            "session_state": {
                "agent_name": "wrcp",
                "rts_model_state": {
                    "context_usage_percentage": 42.5,
                    "model_info": {"context_window_tokens": 1000000},
                },
            },
        }
    else:
        doc = {"session_id": sid, "cwd": "/work", "title": "fresh",
               "updated_at": "2026-09-01T00:00:00Z", "session_state": None}
    _write(cli / f"{sid}.json", json.dumps(doc))
    jsonl = cli / f"{sid}.jsonl"
    _write(jsonl,
           json.dumps({"version": 1, "kind": "Prompt",
                       "data": {"content": [{"kind": "text", "data": "hello"}]}}) + "\n" +
           json.dumps({"version": 1, "kind": "AssistantMessage",
                       "data": {"content": [{"kind": "text", "data": "hi there"}]}}) + "\n" +
           json.dumps({"version": 1, "kind": "ToolResults",
                       "data": {"content": [{"kind": "toolResult"}]}}) + "\n")
    if fresh_jsonl:
        now = time.time()
        os.utime(jsonl, (now, now))
    else:
        old = time.time() - 86400
        os.utime(jsonl, (old, old))
    if with_lock:
        lock = cli / f"{sid}.lock"
        lock.write_text("123", encoding="utf-8")
        if not fresh:
            old = time.time() - 86400  # 1 day old -> stale
            os.utime(lock, (old, old))


def _make_v3(root: Path, sid: str, *, first_turn: bool, fresh: bool) -> None:
    """Create a v3 session; first_turn=False => no contextUsage (not-ready)."""
    ws = root / "abc123hash" / sid
    ws.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    last = now.isoformat().replace("+00:00", "Z") if fresh else "2026-09-01T00:00:00Z"
    session = {
        "id": sid, "title": "v3 title", "agentMode": "wrcp",
        "status": "idle", "lastModifiedAt": last, "parentSessionId": "sess_parent",
        "workspacePaths": ["/work/repo"],
    }
    _write(ws / "session.json", json.dumps(session))
    lines = [
        json.dumps({"payload": {"type": "session_start", "agentType": "wrcp"}}),
        json.dumps({"payload": {"type": "user", "content": "what is 7*7?"}}),
        json.dumps({"payload": {"type": "assistant", "operationType": "Reasoning",
                                "content": "thinking..."}}),
        json.dumps({"payload": {"type": "assistant", "operationType": "Say",
                                "content": "49"}}),
    ]
    if first_turn:
        lines.append(json.dumps({"payload": {
            "type": "session_metadata", "key": "contextUsage",
            "value": {"usagePercentage": 12.34}}}))
    _write(ws / "messages.jsonl", "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# assertions
# --------------------------------------------------------------------------- #
def test_v1v2_normalizes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v1v2(root, "s-active", state=True, fresh=True)
        k = read_session("s-active", root=root)
        assert k.fmt == SessionFormat.V1_V2
        assert k.agent == "wrcp"
        assert k.context_pct == 42.5
        assert k.window == 1000000
        assert k.parent is None
        assert k.alive is True and k.liveness == Liveness.ALIVE


def test_v1v2_null_state_is_not_ready():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v1v2(root, "s-fresh", state=False, fresh=True)
        k = read_session("s-fresh", root=root)  # must NOT crash
        assert k.liveness == Liveness.NOT_READY
        assert k.agent is None and k.context_pct is None


def test_v1v2_stale_lock_is_dead():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v1v2(root, "s-stale", state=True, fresh=False)
        k = read_session("s-stale", root=root)
        assert k.alive is False and k.liveness == Liveness.STALE


def test_v3_normalizes_with_lineage():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3(root, "sess_v3a", first_turn=True, fresh=True)
        k = read_session("sess_v3a", root=root)
        assert k.fmt == SessionFormat.V3
        assert k.agent == "wrcp"
        assert k.context_pct == 12.34   # last contextUsage event
        assert k.window == 1000000
        assert k.parent == "sess_parent"
        assert k.alive is True


def test_v3_pre_first_turn_is_not_ready():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3(root, "sess_v3b", first_turn=False, fresh=True)
        k = read_session("sess_v3b", root=root)
        assert k.liveness == Liveness.NOT_READY
        assert k.context_pct is None


def test_unknown_id_raises():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            read_session("does-not-exist", root=Path(tmp))
            raise AssertionError("expected SessionNotFoundError")
        except SessionNotFoundError:
            pass


def test_unknown_layout_fails_loud():
    with tempfile.TemporaryDirectory() as tmp:
        weird = Path(tmp) / "sess_weird"  # named sess_ but no session.json
        weird.mkdir(parents=True)
        try:
            read_session_path(weird)
            raise AssertionError("expected UnknownSessionFormatError")
        except UnknownSessionFormatError:
            pass


def test_scan_writes_whole_index():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v1v2(root, "s1", state=True, fresh=True)
        _make_v3(root, "sess_2", first_turn=True, fresh=True)
        index_path = Path(tmp) / "index.json"
        doc = scan_knights(root=root, index_path=index_path)
        assert doc["count"] == 2
        assert doc["skipped_corrupt"] == []
        assert doc["skipped_unknown_format"] == []
        assert index_path.exists()
        on_disk = json.loads(index_path.read_text())
        assert {k["session_id"] for k in on_disk["knights"]} == {"s1", "sess_2"}
        # essence/salons are Steward-owned placeholders, never filled by motor.
        assert all(k["essence"] == "" and k["salons"] == [] for k in on_disk["knights"])


def test_mining_preprocess_drops_noise():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3(root, "sess_mine", first_turn=True, fresh=True)
        transcript = root / "abc123hash" / "sess_mine" / "messages.jsonl"
        turns = _detect_and_clean(transcript)
        # user + assistant(Say); Reasoning + session_* dropped.
        assert turns == [("user", "what is 7*7?"), ("assistant", "49")]
        convo = _render_convo(turns)
        assert "### USER" in convo and "### AGENT" in convo
        assert "thinking..." not in convo  # reasoning noise excluded


def test_mining_empty_transcript_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        try:
            _detect_and_clean(empty)
            raise AssertionError("expected MiningError")
        except MiningError:
            pass


# --------------------------------------------------------------------------- #
# C2 (MAJOR) — v1/v2 lockless-but-active session must NOT be false-STALE
# --------------------------------------------------------------------------- #
def test_v1v2_lockless_but_fresh_jsonl_is_alive():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # No .lock at all; updated_at is old; but the transcript is fresh.
        _make_v1v2(root, "s-lockless", state=True, fresh=False,
                   with_lock=False, fresh_jsonl=True)
        k = read_session("s-lockless", root=root)
        assert k.alive is True and k.liveness == Liveness.ALIVE


def test_v1v2_lockless_and_all_stale_is_stale():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # No lock, old jsonl, old updated_at => every signal stale => STALE.
        _make_v1v2(root, "s-dead", state=True, fresh=False,
                   with_lock=False, fresh_jsonl=False)
        k = read_session("s-dead", root=root)
        assert k.alive is False and k.liveness == Liveness.STALE


# --------------------------------------------------------------------------- #
# C5 (MAJOR) — scan skip-classification + fail-loud on systematic format move
# --------------------------------------------------------------------------- #
def test_scan_classifies_corrupt_separately():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v1v2(root, "s-good", state=True, fresh=True)
        # A corrupt v1/v2 json (parse error) — must be skipped_corrupt, not fatal.
        _write(root / "cli" / "s-bad.json", "{ not json ]")
        index_path = Path(tmp) / "index.json"
        doc = scan_knights(root=root, index_path=index_path)
        assert doc["count"] == 1
        assert len(doc["skipped_corrupt"]) == 1
        assert doc["skipped_unknown_format"] == []


def test_scan_fails_loud_on_systematic_unknown_format():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        # ZERO known sessions, but an unknown-layout container exists (a new
        # kiro format): a dir with a json payload that is NOT a v3 sess_*.
        weird = root / "abc123hash" / "conv_newformat"
        weird.mkdir(parents=True)
        (weird / "data.json").write_text("{}", encoding="utf-8")
        try:
            scan_knights(root=root, index_path=Path(tmp) / "index.json")
            raise AssertionError("expected UnknownSessionFormatError")
        except UnknownSessionFormatError:
            pass


# --------------------------------------------------------------------------- #
# C6 (MAJOR) — deterministic tmux_driver coverage via injected pane sequences
# --------------------------------------------------------------------------- #
class _FakeClock:
    """Deterministic monotonic clock advanced by the fake sleeper."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _pane_feeder(sequence):
    """Return a capture_fn yielding successive panes, repeating the last."""
    state = {"i": 0}

    def capture(_sess, _lines):
        idx = min(state["i"], len(sequence) - 1)
        state["i"] += 1
        return sequence[idx]

    return capture


_IDLE = "wrcp · Auto · ◔ 4%\nask a question or describe a task ↵"
_BUSY = "Kiro is working · Type to steer"


def test_watch_no_false_idle_when_busy_never_seen_but_output_changes():
    # Idle prompt is present the whole time, but the pane content keeps
    # CHANGING (agent streaming without ever rendering the busy marker).
    # Must NOT complete until the content stabilizes.
    clock = _FakeClock()
    seq = [
        _IDLE + "\npartial 1",
        _IDLE + "\npartial 12",
        _IDLE + "\npartial 123",
        _IDLE + "\nfinal answer",
        _IDLE + "\nfinal answer",
        _IDLE + "\nfinal answer",
    ]
    res = watch("fake", timeout=1000, poll_interval=1.0, settle_seconds=0.0,
                idle_stable_samples=3,
                capture_fn=_pane_feeder(seq),
                sleep_fn=clock.sleep, now_fn=clock.now)
    assert res["done"] is True and res["reason"] == "idle-stable"


def test_watch_completes_on_busy_to_idle_transition():
    clock = _FakeClock()
    seq = [_BUSY, _BUSY, _IDLE]  # genuine transition
    res = watch("fake", timeout=1000, poll_interval=1.0, settle_seconds=0.0,
                capture_fn=_pane_feeder(seq),
                sleep_fn=clock.sleep, now_fn=clock.now)
    assert res["done"] is True and res["reason"] == "idle-transition"


def test_watch_no_immediate_false_completion_on_prior_idle():
    # THE BLOCKER SCENARIO: pane shows the previous idle prompt at t0 (agent
    # hasn't started), then goes busy, then idle. A snapshot check would return
    # done immediately at sample 0; the transition rule must wait.
    clock = _FakeClock()
    seq = [_IDLE, _BUSY, _BUSY, _IDLE]
    res = watch("fake", timeout=1000, poll_interval=1.0, settle_seconds=0.0,
                idle_stable_samples=99,  # disable the stable path to isolate transition
                capture_fn=_pane_feeder(seq),
                sleep_fn=clock.sleep, now_fn=clock.now)
    assert res["reason"] == "idle-transition"
    # It consumed the busy samples => it did NOT complete on the first idle.
    assert res["elapsed_seconds"] >= 3.0


def test_watch_sentinel_ignores_prompt_echo_while_busy():
    # Sentinel appears in the ECHOED prompt line while the agent is busy; must
    # not be accepted until the pane is no longer busy (POC 02 caveat).
    clock = _FakeClock()
    token = "MOTOR_DONE_49"
    seq = [
        _BUSY + f"\n> reply with {token}",       # echo while busy -> ignore
        _BUSY + f"\n> reply with {token}",
        _IDLE + f"\n{token}: 49",                # now idle + sentinel -> done
    ]
    res = watch("fake", timeout=1000, sentinel=token, poll_interval=1.0,
                settle_seconds=0.0,
                capture_fn=_pane_feeder(seq),
                sleep_fn=clock.sleep, now_fn=clock.now)
    assert res["done"] is True and res["reason"] == "sentinel"
    assert res["elapsed_seconds"] >= 2.0  # did not fire on the busy echoes


def test_watch_timeout_raises():
    clock = _FakeClock()
    seq = [_BUSY]  # never goes idle
    try:
        watch("fake", timeout=5, poll_interval=1.0, settle_seconds=0.0,
              capture_fn=_pane_feeder(seq),
              sleep_fn=clock.sleep, now_fn=clock.now)
        raise AssertionError("expected TmuxTimeoutError")
    except TmuxTimeoutError:
        pass


def test_resolve_new_session_id_single():
    clock = _FakeClock()
    before = {"sess_a"}
    lister = lambda: {"sess_a", "sess_b"}  # exactly one new
    sid = _resolve_new_session_id(before, timeout=10, list_ids_fn=lister,
                                  sleep_fn=clock.sleep, now_fn=clock.now)
    assert sid == "sess_b"


def test_resolve_new_session_id_ambiguous_returns_none():
    clock = _FakeClock()
    before = {"sess_a"}
    lister = lambda: {"sess_a", "sess_b", "sess_c"}  # >1 new => ambiguous
    sid = _resolve_new_session_id(before, timeout=10, list_ids_fn=lister,
                                  sleep_fn=clock.sleep, now_fn=clock.now)
    assert sid is None


def test_resolve_new_session_id_none_written_times_out_to_none():
    clock = _FakeClock()
    before = {"sess_a"}
    lister = lambda: {"sess_a"}  # nothing new ever appears
    sid = _resolve_new_session_id(before, timeout=5, list_ids_fn=lister,
                                  sleep_fn=clock.sleep, now_fn=clock.now)
    assert sid is None


# --------------------------------------------------------------------------- #
# C7 (MINOR) — mine() subprocess assembly + clean-convo landing (stubbed)
# --------------------------------------------------------------------------- #
def test_mine_dry_run_command_and_landing(monkeypatch=None):
    from motor import mining
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3(root, "sess_mine", first_turn=True, fresh=True)
        transcript = root / "abc123hash" / "sess_mine" / "messages.jsonl"
        palace = Path(tmp) / "palace"
        os.environ["TMUX_MAINBRAIN_PALACE"] = str(palace)

        captured = {}

        class _FakeCompleted:
            returncode = 0
            stderr = b""
            stdout = b""

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["timeout"] = kwargs.get("timeout")
            return _FakeCompleted()

        original_run = mining.subprocess.run
        mining.subprocess.run = _fake_run
        try:
            res = mining.mine(str(transcript), wing="ptp", room="packaging",
                              dry_run=True)
        finally:
            mining.subprocess.run = original_run
            os.environ.pop("TMUX_MAINBRAIN_PALACE", None)

        cmd = captured["cmd"]
        assert "mempalace" in cmd[0]
        assert "--palace" in cmd and "--mode" in cmd and "convos" in cmd
        assert "--wing" in cmd and "ptp/packaging" in cmd
        assert "--dry-run" in cmd
        assert captured["timeout"] is not None  # bounded
        # Clean convo landed under palace/archmaester/ptp/packaging/
        landing = Path(res["convo_path"])
        assert landing.exists()
        assert landing.parent == palace / "archmaester" / "ptp" / "packaging"
        assert res["turns"] == 2


def test_deliver_empty_prompt_refused():
    try:
        tmux_driver.deliver("whatever", "")
        raise AssertionError("expected DeliveryError")
    except DeliveryError:
        pass


# --------------------------------------------------------------------------- #
# read_turns (content companion to read_session)
# --------------------------------------------------------------------------- #
def _make_v3_turns(root: Path, sid: str) -> None:
    """A v3 session shaped like the REAL layout: user sits BEFORE turn_start,
    assistant speech is streamed across multiple chunks, tools interleave.
    Two full turns so --last and --role can be exercised.
    """
    ws = root / "abc123hash" / sid
    ws.mkdir(parents=True, exist_ok=True)
    session = {"id": sid, "title": "t", "agentMode": "wrcp", "status": "idle",
               "lastModifiedAt": "2026-09-01T00:00:00Z"}
    _write(ws / "session.json", json.dumps(session))
    lines = [
        {"timestamp": "t0", "payload": {"type": "session_start"}},
        {"timestamp": "t1", "payload": {"type": "user", "content": "first question"}},
        {"timestamp": "t2", "payload": {"type": "turn_start", "executionId": "e1"}},
        {"timestamp": "t3", "payload": {"type": "assistant", "content": "Ans"}},
        {"timestamp": "t4", "payload": {"type": "assistant", "content": "wer one"}},
        {"timestamp": "t5", "payload": {"type": "tool_call", "content": "IGNORE"}},
        {"timestamp": "t6", "payload": {"type": "tool_result", "content": "IGNORE"}},
        {"timestamp": "t7", "payload": {"type": "turn_end", "stopReason": "end"}},
        {"timestamp": "t8", "payload": {"type": "user", "content": "second question"}},
        {"timestamp": "t9", "payload": {"type": "turn_start", "executionId": "e2"}},
        {"timestamp": "t10", "payload": {"type": "assistant", "content": "Answer two"}},
        {"timestamp": "t11", "payload": {"type": "session_metadata",
                                         "key": "contextUsage",
                                         "value": {"usagePercentage": 5.0}}},
        {"timestamp": "t12", "payload": {"type": "turn_end", "stopReason": "end"}},
    ]
    _write(ws / "messages.jsonl", "\n".join(json.dumps(x) for x in lines) + "\n")


def test_read_turns_v3_default_last_assistant():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        out = read_turns("sess_rt", root=root)  # default last=1 role=assistant
        assert out["count"] == 1, out
        t = out["turns"][0]
        assert t["role"] == "assistant"
        assert t["text"] == "Answer two", t  # last assistant speech
        assert "IGNORE" not in t["text"]


def test_read_turns_v3_concatenates_streamed_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        out = read_turns("sess_rt", last=2, role="assistant", root=root)
        texts = [t["text"] for t in out["turns"]]
        # First turn's two chunks concatenated into one clean speech.
        assert texts == ["Answer one", "Answer two"], texts


def test_read_turns_v3_role_all_interleaves_user_before_turn():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        out = read_turns("sess_rt", last=4, role="all", root=root)
        roles = [(t["role"], t["text"]) for t in out["turns"]]
        # user (before turn_start) must be captured and ordered before its turn.
        assert roles == [
            ("user", "first question"),
            ("assistant", "Answer one"),
            ("user", "second question"),
            ("assistant", "Answer two"),
        ], roles


def test_read_turns_v3_role_user_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        out = read_turns("sess_rt", last=5, role="user", root=root)
        assert [t["text"] for t in out["turns"]] == ["first question", "second question"]


def test_read_turns_v1v2_excludes_tools():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v1v2(root, "leg123", state=True, fresh=True)
        out = read_turns("leg123", last=9, role="all", root=root)
        pairs = [(t["role"], t["text"]) for t in out["turns"]]
        assert pairs == [("user", "hello"), ("assistant", "hi there")], pairs
        # the ToolResults line contributed nothing.


def test_read_turns_invalid_role_raises():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        try:
            read_turns("sess_rt", role="bogus", root=root)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_read_turns_nonpositive_last_raises():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        try:
            read_turns("sess_rt", last=0, root=root)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


def test_read_turns_missing_session_raises():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            read_turns("sess_absent", root=Path(tmp))
            raise AssertionError("expected SessionNotFoundError")
        except SessionNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# bare runner (no pytest dependency)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # report + count; do not abort the suite
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
