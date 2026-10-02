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
# .env.secrets injection (_secret_env_flags) — null-tolerant, first-= split
# --------------------------------------------------------------------------- #
def test_secret_env_flags_absent_file_is_noop():
    # Point the repo root at an empty tmpdir (no .env.secrets) -> [] , no crash.
    import os as _os
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        prev = _os.environ.get("TMUX_MAINBRAIN_ROOT")
        _os.environ["TMUX_MAINBRAIN_ROOT"] = tmp
        try:
            assert td._secret_env_flags() == []
        finally:
            if prev is None:
                _os.environ.pop("TMUX_MAINBRAIN_ROOT", None)
            else:
                _os.environ["TMUX_MAINBRAIN_ROOT"] = prev


def test_secret_env_flags_parses_export_comments_and_first_equals():
    import os as _os
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env.secrets").write_text(
            "# a comment\n"
            "\n"
            "export JIRA_TOKEN=abc123\n"
            "PLAIN_KEY=def456\n"
            'GERRIT_INSTANCES={"internal": {"url": "https://x", "token": "t=t"}}\n',
            encoding="utf-8",
        )
        prev = _os.environ.get("TMUX_MAINBRAIN_ROOT")
        _os.environ["TMUX_MAINBRAIN_ROOT"] = tmp
        try:
            flags = td._secret_env_flags()
        finally:
            if prev is None:
                _os.environ.pop("TMUX_MAINBRAIN_ROOT", None)
            else:
                _os.environ["TMUX_MAINBRAIN_ROOT"] = prev
        # -e KEY=VAL pairs, comments/blanks skipped, first-'=' split preserves
        # a value that itself contains '='.
        assert flags[0] == "-e" and flags[1] == "JIRA_TOKEN=abc123"
        assert "-e" in flags and "PLAIN_KEY=def456" in flags
        gi = [flags[i + 1] for i in range(0, len(flags), 2)
              if flags[i + 1].startswith("GERRIT_INSTANCES=")][0]
        assert gi == 'GERRIT_INSTANCES={"internal": {"url": "https://x", "token": "t=t"}}'
        assert len([f for f in flags if f == "-e"]) == 3  # 3 real vars, comment dropped


# --------------------------------------------------------------------------- #
# capacitate_from_repo — shape detection + producers (local fixtures, NO net)
# --------------------------------------------------------------------------- #
def _fake_clone(fixture: Path):
    """Return a clone_fn that copies a local fixture dir into the clone target."""
    import shutil as _sh

    def _clone(_url, dest, _branch):
        _sh.copytree(fixture, dest)
    return _clone


def _assert_valid_front_matter(agent_text: str) -> None:
    """The agent's YAML front matter must parse (regression: unquoted colon)."""
    assert agent_text.startswith("---\n")
    fm = agent_text.split("---", 2)[1]
    try:
        import yaml  # type: ignore
    except ImportError:
        # No PyYAML: at least assert the description is quoted (the failure mode).
        for line in fm.splitlines():
            if line.startswith("description:"):
                val = line.split(":", 1)[1].strip()
                assert val.startswith('"') and val.endswith('"'), \
                    f"description must be quoted, got: {val}"
        return
    parsed = yaml.safe_load(fm)
    assert isinstance(parsed, dict) and parsed.get("name")


def _make_power_fixture_legacy(root: Path) -> Path:
    """A legacy POWER.md bundle (like design-system-scaffold)."""
    d = root / "src-power"
    (d / "steering").mkdir(parents=True)
    (d / "POWER.md").write_text(
        "---\nname: design-thing\ndisplayName: Design Thing\n"
        "description: A design system power.\nkeywords:\n"
        "  - design-system\n  - ui\n  - theming\nauthor: AWS\nversion: 1.0.0\n---\n\n"
        "# Design Thing\nGuidance body.\n", encoding="utf-8")
    (d / "steering" / "guide.md").write_text("# guide\n", encoding="utf-8")
    return d


def _make_power_fixture_plugin(root: Path) -> Path:
    """A modern plugin.json power."""
    d = root / "src-plugin"
    (d / "skills" / "setup").mkdir(parents=True)
    (d / "plugin.json").write_text(json.dumps({
        "name": "supa", "version": "1.0.0", "description": "x",
        "keywords": ["database", "postgres"]}), encoding="utf-8")
    (d / "skills" / "setup" / "SKILL.md").write_text(
        "---\nname: setup\n---\n# setup\n", encoding="utf-8")
    return d


def _make_agents_fixture(root: Path) -> Path:
    d = root / "src-agents"
    (d / ".kiro" / "agents").mkdir(parents=True)
    (d / ".kiro" / "agents" / "coder.md").write_text(
        "---\nname: coder\ndescription: A coder.\n---\n# coder\nYou write code.\n",
        encoding="utf-8")
    (d / "README.md").write_text("# repo\n", encoding="utf-8")
    return d


def _make_code_fixture(root: Path) -> Path:
    d = root / "src-code"
    (d / "src").mkdir(parents=True)
    (d / "README.md").write_text(
        "# Cool Lib\n\nA fast JSON parser for embedded systems.\n\n"
        "## Install\n`npm i cool-lib`\n", encoding="utf-8")
    (d / "package.json").write_text('{"name":"cool-lib"}', encoding="utf-8")
    (d / "src" / "index.js").write_text("module.exports = {}\n", encoding="utf-8")
    return d


def test_detect_shape_power_legacy_and_plugin():
    from motor.capacitate import detect_shape, Shape
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert detect_shape(_make_power_fixture_legacy(root)) == Shape.POWER
        assert detect_shape(_make_power_fixture_plugin(root)) == Shape.POWER


def test_detect_shape_agents_and_code():
    from motor.capacitate import detect_shape, Shape
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert detect_shape(_make_agents_fixture(root)) == Shape.AGENTS
        assert detect_shape(_make_code_fixture(root)) == Shape.CODE


def test_capacitate_power_legacy_forges_file_bind():
    from motor.capacitate import capacitate_from_repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_power_fixture_legacy(root)
        out = capacitate_from_repo("https://x/design-thing", dest=str(root / "forge"),
                                   clone_fn=_fake_clone(fixture))
        assert out["shape"] == "power"
        assert out["bind"] == "file-resources"  # cheap FILE bind, not injection
        assert out["manifest"] == "POWER.md"
        assert "design-system" in out["keywords"]
        # both a power file and a binding agent were written
        power_file = Path(out["power_file"])
        agent_file = Path(out["agent_file"])
        assert power_file.is_file() and agent_file.is_file()
        assert power_file.parent.name == "powers"
        assert agent_file.parent.name == "agents"
        # the agent loads the power BY FILE via resources (no prompt injection)
        agent_text = agent_file.read_text("utf-8")
        assert "resources:" in agent_text
        assert f"file://{power_file}" in agent_text
        # the forged agent front matter must be VALID (description quoted, etc.)
        _assert_valid_front_matter(agent_text)
        # the consolidated power carries the bundle's steering guidance
        assert "guide" in power_file.read_text("utf-8")


def test_capacitate_power_plugin_forges_file_bind():
    from motor.capacitate import capacitate_from_repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_power_fixture_plugin(root)
        out = capacitate_from_repo("https://x/supa", dest=str(root / "forge"),
                                   clone_fn=_fake_clone(fixture))
        assert out["shape"] == "power" and out["bind"] == "file-resources"
        assert out["manifest"] == "plugin.json"
        assert "database" in out["keywords"]
        assert Path(out["power_file"]).is_file()
        # skills/*/SKILL.md folded into the consolidated power file
        assert "setup" in Path(out["power_file"]).read_text("utf-8")


def test_capacitate_agents_forges_file_cheap_agent():
    from motor.capacitate import capacitate_from_repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_agents_fixture(root)
        out = capacitate_from_repo("https://x/my-agents", dest=str(root / "forge"),
                                   clone_fn=_fake_clone(fixture))
        assert out["shape"] == "agents"
        assert out["bind"] == "file-agent"
        artifact = Path(out["agent_file"])
        assert artifact.is_file() and artifact.name.startswith("ephemeral-")
        assert artifact.parent.name == "agents"
        body = artifact.read_text("utf-8")
        assert body.startswith("---") and "name: ephemeral-" in body
        assert "You write code." in body  # persona carried over IN the file


def test_capacitate_code_refuses_cleanly():
    from motor.capacitate import capacitate_from_repo
    from motor.errors import CapacitateError
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_code_fixture(root)
        try:
            capacitate_from_repo("https://x/cool-lib", dest=str(root / "forge"),
                                 clone_fn=_fake_clone(fixture))
            raise AssertionError("expected CapacitateError (code shape refused)")
        except CapacitateError as exc:
            assert "out of scope" in str(exc).lower()


def test_capacitate_dry_run_writes_nothing():
    from motor.capacitate import capacitate_from_repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_power_fixture_legacy(root)
        out = capacitate_from_repo("https://x/design-thing", dest=str(root / "forge"),
                                   dry_run=True, clone_fn=_fake_clone(fixture))
        assert out["dry_run"] is True
        assert not Path(out["power_file"]).exists()  # nothing written
        assert not Path(out["agent_file"]).exists()


def test_capacitate_empty_repo_raises_clean():
    from motor.capacitate import capacitate_from_repo
    from motor.errors import CapacitateError
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def _clone_empty(_u, dest, _b):
            dest.mkdir(parents=True)  # produce an EMPTY clone

        try:
            capacitate_from_repo("https://x/empty", dest=str(root / "forge"),
                                 clone_fn=_clone_empty)
            raise AssertionError("expected CapacitateError")
        except CapacitateError:
            pass


def test_capacitate_bad_url_raises_clean():
    from motor.capacitate import capacitate_from_repo
    from motor.errors import CapacitateError
    try:
        capacitate_from_repo("")
        raise AssertionError("expected CapacitateError")
    except CapacitateError:
        pass


def test_capacitate_journey_places_under_journey_artifacts():
    """--journey routes forged ephemerals to journeys/<id>/artifacts/.kiro/…"""
    import os as _os
    from motor.capacitate import capacitate_from_repo
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture = _make_power_fixture_legacy(root)
        prev = _os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        _os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(root / "journeys")
        try:
            out = capacitate_from_repo("https://x/design-thing",
                                       journey="journey-test",
                                       clone_fn=_fake_clone(fixture))
        finally:
            if prev is None:
                _os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                _os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        agent_file = Path(out["agent_file"])
        power_file = Path(out["power_file"])
        # both land under journeys/journey-test/artifacts/.kiro/…
        assert "journey-test" in agent_file.parts
        assert agent_file.parts[-3:] == (".kiro", "agents",
                                         "ephemeral-design-thing.md")
        assert power_file.parts[-3:] == (".kiro", "powers",
                                         "ephemeral-power-design-thing.md")
        assert "artifacts" in agent_file.parts
        # the cheap file bind points at the power's journey path
        assert f"file://{power_file}" in agent_file.read_text("utf-8")
        # the scratch clone stays OUT of the journey (in .cap-forge)
        assert "journey-test" not in out["cloned_to"]


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
# read_turns --since (incremental GET) + --spill + search + read_verdict
# --------------------------------------------------------------------------- #
def _append_verdict_turn(root: Path, sid: str, *, marker: bool,
                         body: str = "the verdict body\nwith two lines") -> None:
    """Append a second full turn to the sess_rt fixture, optionally carrying a
    FINAL VERDICT marker with a multi-line body."""
    ws = root / "abc123hash" / sid
    transcript = ws / "messages.jsonl"
    speech = ("preamble that must be excluded.\n\nFINAL VERDICT\n" + body) \
        if marker else "a plain closing answer with no marker at all"
    extra = [
        {"timestamp": "z0", "payload": {"type": "user", "content": "conclude"}},
        {"timestamp": "z1", "payload": {"type": "turn_start", "executionId": "e3"}},
        {"timestamp": "z2", "payload": {"type": "assistant", "content": speech}},
        {"timestamp": "z3", "payload": {"type": "turn_end", "stopReason": "end"}},
    ]
    with transcript.open("a", encoding="utf-8") as handle:
        for row in extra:
            handle.write(json.dumps(row) + "\n")


def test_read_turns_since_returns_only_delta():
    from motor.sessions import read_turns as rt
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        first = rt("sess_rt", last=99, role="all", root=root)
        cursor = first["cursor"]
        assert cursor > 0
        # Append a new turn; a --since read must return ONLY the new material.
        _append_verdict_turn(root, "sess_rt", marker=False)
        delta = rt("sess_rt", last=99, role="all", since=cursor, root=root)
        texts = [t["text"] for t in delta["turns"]]
        assert "conclude" in texts  # the new user turn
        assert "first question" not in texts  # old turns NOT re-returned
        assert delta["cursor"] > cursor


def test_read_turns_spill_writes_file():
    from motor.sessions import read_turns as rt
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        spill = Path(tmp) / "cut" / "turns.md"
        out = rt("sess_rt", last=2, role="all", spill=str(spill), root=root)
        assert out["spill_path"] == str(spill)
        assert spill.exists()
        content = spill.read_text(encoding="utf-8")
        assert "### AGENT" in content
        assert "Answer two" in content


def test_read_verdict_extracts_block_intact():
    from motor.sessions import read_verdict
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        _append_verdict_turn(root, "sess_rt", marker=True,
                             body="line A\nline B\nline C")
        res = read_verdict("sess_rt", timeout=0, interval=0, root=root)
        assert res["found"] is True, res
        # Only the block, intact (newlines preserved), preamble excluded.
        assert res["verdict"].startswith("FINAL VERDICT")
        assert "line A\nline B\nline C" in res["verdict"]
        assert "preamble" not in res["verdict"]


def test_read_verdict_timeout_reports_no_marker():
    from motor.sessions import read_verdict
    clock = _FakeClock()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        _append_verdict_turn(root, "sess_rt", marker=False)
        # Advance the clock well past any growth so state == no-marker.
        res = read_verdict("sess_rt", timeout=5, interval=1, root=root,
                           sleep_fn=clock.sleep, now_fn=clock.now)
        assert res["found"] is False
        assert res["state"] in ("no-marker", "in-progress")
        assert res["tail"]  # a tail is offered for the nudge decision


def test_read_verdict_picks_last_marker_when_repeated():
    from motor.sessions import read_verdict
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        # Two verdict turns: read_verdict must return the LAST one.
        _append_verdict_turn(root, "sess_rt", marker=True, body="OLD")
        _append_verdict_turn(root, "sess_rt", marker=True, body="NEW")
        res = read_verdict("sess_rt", timeout=0, interval=0, root=root)
        assert res["found"] is True
        assert "NEW" in res["verdict"] and "OLD" not in res["verdict"]


def test_search_turns_finds_across_sessions():
    from motor.sessions import search_turns
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v3_turns(root, "sess_a")
        _make_v1v2(root, "leg1", state=True, fresh=True)  # has "hi there"
        hits = search_turns("hi there", root=root)
        assert hits["count"] >= 1
        assert any(m["session_id"] == "leg1" for m in hits["matches"])
        snip = hits["matches"][0]["snippet"]
        assert "hi there" in snip


def test_search_turns_role_filter_and_case():
    from motor.sessions import search_turns
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v3_turns(root, "sess_a")
        # 'Answer two' is an assistant turn; user filter must NOT match it.
        assert search_turns("Answer two", role="user", root=root)["count"] == 0
        assert search_turns("Answer two", role="assistant", root=root)["count"] == 1
        # case-insensitive by default
        assert search_turns("answer TWO", root=root)["count"] == 1


def test_read_turns_since_empty_when_no_new_data():
    from motor.sessions import read_turns as rt
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_v3_turns(root, "sess_rt")
        first = rt("sess_rt", last=99, role="all", root=root)
        again = rt("sess_rt", last=99, role="all", since=first["cursor"],
                   root=root)
        assert again["count"] == 0  # nothing appended => no delta
        assert again["cursor"] == first["cursor"]


# --------------------------------------------------------------------------- #
# lineage — automatic parent-edge recording into journeys/<id>/meta.json
# --------------------------------------------------------------------------- #
_ROOT_SID = "sess_root_king"


def _make_journey_meta(journeys_root: Path, journey_id: str,
                       knights: list) -> Path:
    """Write a journey meta.json shaped like the real one (tree_root L1 +
    parenting_rule + tui + knights[]). Returns the meta.json path."""
    jdir = journeys_root / journey_id
    jdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "journey_id": journey_id,
        "tui": "ENABLED",
        "goal": "test journey",
        "parenting_rule": "parent = who COMMANDS this knight (the requester).",
        "tree_root": {"role": "king", "level": 1, "session_id": _ROOT_SID},
        "knights": knights,
    }
    meta = jdir / "meta.json"
    meta.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return meta


def _l2_knight(sid: str, *, role: str = "archmaester",
               agent: str = "archmaester") -> dict:
    return {"role": role, "agent": agent, "session_id": sid,
            "tmux_session": f"{role}-tui", "parent": _ROOT_SID, "level": 2,
            "resolved": True, "note": "seed L2 knight"}


def test_lineage_records_child_of_tree_root_at_level_2():
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        meta = _make_journey_meta(journeys, "j", knights=[])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            recorded, warning = record_lineage(
                "j", "sess_child", "child-tui", "some-agent",
                parent_session_id=_ROOT_SID)
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert recorded is True and warning is None
        doc = json.loads(meta.read_text())
        # tree_root, parenting_rule, tui preserved verbatim.
        assert doc["tree_root"]["session_id"] == _ROOT_SID
        assert doc["parenting_rule"].startswith("parent = who COMMANDS")
        assert doc["tui"] == "ENABLED"
        knight = next(k for k in doc["knights"] if k["session_id"] == "sess_child")
        assert knight["parent"] == _ROOT_SID
        assert knight["level"] == 2  # child of the L1 root
        assert knight["role"] == "some-agent"  # role defaults to agent name
        assert knight["tmux_session"] == "child-tui"
        assert knight["resolved"] is True
        assert "spawned via motor --parent" in knight["note"]


def test_lineage_records_L3_child_of_existing_L2_knight():
    """THE L3 CASE: parent is an existing level-2 knight => child level 3."""
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        seed = _l2_knight("sess_arch", role="archmaester", agent="archmaester")
        meta = _make_journey_meta(journeys, "j", knights=[seed])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            recorded, warning = record_lineage(
                "j", "sess_subknight", "sub-tui", "sub-agent",
                parent_session_id="sess_arch", role="sub-knight")
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert recorded is True and warning is None
        doc = json.loads(meta.read_text())
        # The seed L2 knight is untouched.
        arch = next(k for k in doc["knights"] if k["session_id"] == "sess_arch")
        assert arch["level"] == 2 and arch["note"] == "seed L2 knight"
        child = next(k for k in doc["knights"]
                     if k["session_id"] == "sess_subknight")
        assert child["parent"] == "sess_arch"
        assert child["level"] == 3  # <-- the whole point
        assert child["role"] == "sub-knight"


def test_lineage_parent_not_found_records_null_level_with_warning():
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        meta = _make_journey_meta(journeys, "j", knights=[])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            recorded, warning = record_lineage(
                "j", "sess_orphan", "orphan-tui", "orphan-agent",
                parent_session_id="sess_ghost")
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        # Recorded (True) with a warning; level is null, parent still set.
        assert recorded is True
        assert warning is not None and "not found" in warning
        doc = json.loads(meta.read_text())
        knight = next(k for k in doc["knights"]
                      if k["session_id"] == "sess_orphan")
        assert knight["parent"] == "sess_ghost"
        assert knight["level"] is None  # NOT invented
        assert "level=null" in knight["note"]


def test_lineage_missing_journey_meta_fails_soft():
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"  # no journey dir/meta at all
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            recorded, warning = record_lineage(
                "j-absent", "sess_x", "x-tui", "x-agent",
                parent_session_id=_ROOT_SID)
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert recorded is False
        assert warning is not None and "unreadable" in warning


def test_lineage_corrupt_journey_meta_fails_soft():
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        jdir = journeys / "j"
        jdir.mkdir(parents=True)
        (jdir / "meta.json").write_text("{ not valid json ]", encoding="utf-8")
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            recorded, warning = record_lineage(
                "j", "sess_x", "x-tui", "x-agent",
                parent_session_id=_ROOT_SID)
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert recorded is False
        assert warning is not None and "unparseable" in warning


def test_lineage_idempotent_update_not_duplicate():
    from motor.lineage import record_lineage
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        meta = _make_journey_meta(journeys, "j", knights=[])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        try:
            record_lineage("j", "sess_dup", "old-tui", "old-agent",
                           parent_session_id=_ROOT_SID)
            # Same session_id again with new pointer fields.
            recorded, _ = record_lineage("j", "sess_dup", "new-tui", "new-agent",
                                         parent_session_id=_ROOT_SID,
                                         role="updated-role")
        finally:
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert recorded is True
        doc = json.loads(meta.read_text())
        dups = [k for k in doc["knights"] if k["session_id"] == "sess_dup"]
        assert len(dups) == 1  # updated, not duplicated
        assert dups[0]["tmux_session"] == "new-tui"
        assert dups[0]["agent"] == "new-agent"
        assert dups[0]["role"] == "updated-role"


# --- spawn/resume lineage folding (stubbed tmux — never a real session) ----- #
def _stub_tmux_spawn(monkeypatch_target, resolved_sid):
    """Neutralize the real tmux/kiro seams in tmux_driver so spawn/resume run
    their lineage logic WITHOUT touching tmux. Returns a restore callable."""
    td = monkeypatch_target
    saved = {
        "_session_exists": td._session_exists,
        "_tmux": td._tmux,
        "_wait_until_ready": td._wait_until_ready,
        "_resolve_new_session_id": td._resolve_new_session_id,
        "_secret_env_flags": td._secret_env_flags,
        "_snapshot_session_ids": td._snapshot_session_ids,
    }
    td._session_exists = lambda _name: False
    td._tmux = lambda *a, **k: ""
    td._wait_until_ready = lambda *a, **k: None
    td._secret_env_flags = lambda: []
    td._snapshot_session_ids = lambda *a, **k: set()
    td._resolve_new_session_id = lambda *a, **k: resolved_sid

    def _restore():
        for name, fn in saved.items():
            setattr(td, name, fn)
    return _restore


def test_spawn_backward_compat_no_lineage_args():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        restore = _stub_tmux_spawn(td, "sess_new")
        try:
            res = td.spawn("some-agent", tmux_session="t", cwd=tmp)
        finally:
            restore()
        # Unchanged shape + the always-present flag; NO warning, NO meta write.
        assert res["session_id"] == "sess_new"
        assert res["resolved"] is True
        assert res["lineage_recorded"] is False
        assert "lineage_warning" not in res


def test_spawn_with_parent_and_journey_records_l2(monkeypatch=None):
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        meta = _make_journey_meta(journeys, "j", knights=[])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        restore = _stub_tmux_spawn(td, "sess_new")
        try:
            res = td.spawn("child-agent", tmux_session="child-tui", cwd=tmp,
                           parent=_ROOT_SID, journey="j", role="child")
        finally:
            restore()
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert res["lineage_recorded"] is True
        assert "lineage_warning" not in res
        doc = json.loads(meta.read_text())
        knight = next(k for k in doc["knights"] if k["session_id"] == "sess_new")
        assert knight["parent"] == _ROOT_SID and knight["level"] == 2
        assert knight["role"] == "child"


def test_spawn_unresolved_session_id_reports_lineage_warning():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        _make_journey_meta(journeys, "j", knights=[])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        restore = _stub_tmux_spawn(td, None)  # id NOT resolved
        try:
            res = td.spawn("child-agent", tmux_session="child-tui", cwd=tmp,
                           parent=_ROOT_SID, journey="j")
        finally:
            restore()
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert res["resolved"] is False
        assert res["lineage_recorded"] is False
        assert "unresolved" in res["lineage_warning"]


def test_spawn_only_parent_without_journey_warns_records_nothing():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        restore = _stub_tmux_spawn(td, "sess_new")
        try:
            res = td.spawn("child-agent", tmux_session="t", cwd=tmp,
                           parent=_ROOT_SID)  # no --journey
        finally:
            restore()
        assert res["lineage_recorded"] is False
        assert "journey" in res["lineage_warning"]


def test_resume_with_parent_and_journey_records():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        journeys = Path(tmp) / "journeys"
        seed = _l2_knight("sess_arch")
        meta = _make_journey_meta(journeys, "j", knights=[seed])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        restore = _stub_tmux_spawn(td, "ignored-for-resume")
        try:
            # resume records under the resumed knight's OWN id (kiro_session_id).
            res = td.resume("sess_resumed", tmux_session="r-tui", cwd=tmp,
                            parent="sess_arch", journey="j", role="sub",
                            agent="sub-agent")
        finally:
            restore()
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert res["lineage_recorded"] is True
        doc = json.loads(meta.read_text())
        knight = next(k for k in doc["knights"]
                      if k["session_id"] == "sess_resumed")
        assert knight["parent"] == "sess_arch" and knight["level"] == 3


# --------------------------------------------------------------------------- #
# resume_clean — stale-lock-aware resume (kiro has no native lock/force flag)
# --------------------------------------------------------------------------- #
def _make_v3_with_lock(root: Path, sid: str, *, lock: str = None) -> Path:
    """Build a v3 session dir (first-turn so it resolves), optionally with a
    ``.lock`` whose raw content is ``lock`` (a JSON string, or None => no lock).
    Returns the session dir."""
    _make_v3(root, sid, first_turn=True, fresh=True)
    ws = root / "abc123hash" / sid
    if lock is not None:
        (ws / ".lock").write_text(lock, encoding="utf-8")
    return ws


def test_resume_clean_lock_absent_resumes_normally():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v3_with_lock(root, "sess_nolock", lock=None)  # no .lock
        restore = _stub_tmux_spawn(td, "ignored")
        try:
            res = td.resume_clean("sess_nolock", tmux_session="rc", cwd=tmp,
                                  root=root,
                                  pid_alive_fn=lambda _pid: True)  # never consulted
        finally:
            restore()
        assert res["resumed"] is True
        assert res["removed_stale_lock"] is False
        assert res["tmux_session"] == "rc"
        assert res["session_id"] == "sess_nolock"


def test_resume_clean_dead_pid_removes_lock_and_resumes():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        ws = _make_v3_with_lock(root, "sess_dead",
                                lock=json.dumps({"pid": 424242,
                                                 "started_at": "t"}))
        lock_path = ws / ".lock"
        assert lock_path.exists()
        restore = _stub_tmux_spawn(td, "ignored")
        try:
            res = td.resume_clean("sess_dead", tmux_session="rc", cwd=tmp,
                                  root=root,
                                  pid_alive_fn=lambda _pid: False)  # DEAD
        finally:
            restore()
        assert res["resumed"] is True
        assert res["removed_stale_lock"] is True
        assert res["pid"] == 424242
        assert not lock_path.exists()  # stale lock cleared


def test_resume_clean_alive_pid_refuses_and_keeps_lock():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        ws = _make_v3_with_lock(root, "sess_live",
                                lock=json.dumps({"pid": 1927179}))
        lock_path = ws / ".lock"
        # If resume were (wrongly) attempted, the tmux seam would be hit; assert
        # it is NOT by making _tmux raise if called.
        def _boom(*_a, **_k):
            raise AssertionError("resume must NOT be attempted for a live lock")
        saved_tmux = td._tmux
        td._tmux = _boom
        try:
            res = td.resume_clean("sess_live", tmux_session="rc", cwd=tmp,
                                  root=root,
                                  pid_alive_fn=lambda _pid: True)  # ALIVE
        finally:
            td._tmux = saved_tmux
        assert res["resumed"] is False
        assert res["reason"] == "held_by_live"
        assert res["pid"] == 1927179
        assert res["tmux_session"] is None
        assert res["removed_stale_lock"] is False
        assert lock_path.exists()  # NEVER removed


def test_resume_clean_unparseable_pid_refuses_unknown_lock():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        ws = _make_v3_with_lock(root, "sess_junk", lock="{ not json ]")
        lock_path = ws / ".lock"
        restore = _stub_tmux_spawn(td, "ignored")
        try:
            res = td.resume_clean("sess_junk", tmux_session="rc", cwd=tmp,
                                  root=root,
                                  pid_alive_fn=lambda _pid: False)
        finally:
            restore()
        assert res["resumed"] is False
        assert res["reason"] == "unknown_lock"
        assert res["tmux_session"] is None
        assert res["removed_stale_lock"] is False
        assert lock_path.exists()  # NOT removed (safer to refuse than guess)


def test_resume_clean_dead_pid_with_lineage_records():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        _make_v3_with_lock(root, "sess_dead2",
                           lock=json.dumps({"pid": 999999}))
        journeys = Path(tmp) / "journeys"
        seed = _l2_knight("sess_arch")
        meta = _make_journey_meta(journeys, "j", knights=[seed])
        prev = os.environ.get("TMUX_MAINBRAIN_JOURNEYS")
        os.environ["TMUX_MAINBRAIN_JOURNEYS"] = str(journeys)
        restore = _stub_tmux_spawn(td, "ignored")
        try:
            res = td.resume_clean("sess_dead2", tmux_session="rc", cwd=tmp,
                                  root=root, parent="sess_arch", journey="j",
                                  role="sub", agent="sub-agent",
                                  pid_alive_fn=lambda _pid: False)
        finally:
            restore()
            if prev is None:
                os.environ.pop("TMUX_MAINBRAIN_JOURNEYS", None)
            else:
                os.environ["TMUX_MAINBRAIN_JOURNEYS"] = prev
        assert res["resumed"] is True
        assert res["removed_stale_lock"] is True
        assert res["lineage_recorded"] is True
        doc = json.loads(meta.read_text())
        knight = next(k for k in doc["knights"]
                      if k["session_id"] == "sess_dead2")
        # child of the L2 archmaester => level 3, parent set.
        assert knight["parent"] == "sess_arch" and knight["level"] == 3


def test_resume_clean_missing_session_dir_fails_soft():
    from motor import tmux_driver as td
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"  # empty; session does not exist
        restore = _stub_tmux_spawn(td, "ignored")
        try:
            res = td.resume_clean("sess_absent", tmux_session="rc", cwd=tmp,
                                  root=root,
                                  pid_alive_fn=lambda _pid: False)
        finally:
            restore()
        # Fail-soft: clear reason, no exception, no resume.
        assert res["resumed"] is False
        assert res["reason"] == "session_not_found"
        assert res["tmux_session"] is None
        assert res["removed_stale_lock"] is False


def test_resume_clean_default_pid_probe_reads_proc():
    """The default pid-liveness seam probes /proc; a nonexistent pid is dead."""
    from motor.lockcheck import default_pid_alive
    # PID 1 (init) is always alive on Linux; a huge pid is not present.
    assert default_pid_alive(1) is True
    assert default_pid_alive(2_147_480_000) is False


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
