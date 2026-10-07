"""Unit tests for journey.py — slugify, collision reject, meta shape, id
capture fallbacks, cleanup-on-failure. All motor/tmux/fs effects are injected;
no real king is ever spawned."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import journey as j  # noqa: E402


# --- slugify ----------------------------------------------------------------

def test_slugify_basic():
    assert j.slugify("My Cool Journey") == "my-cool-journey"
    assert j.slugify("  Trim  Me  ") == "trim-me"
    assert j.slugify("weird!!!chars@@@here") == "weird-chars-here"
    assert j.slugify("already-slug") == "already-slug"


def test_slugify_rejects_empty_or_junk():
    for bad in ["", "   ", "!!!", "@#$"]:
        with pytest.raises(j.JourneyError):
            j.slugify(bad)


# --- session id parsing (matches the EMPIRICAL kiro banner) -----------------

def test_parse_session_id_from_banner():
    banner = (
        "Did you know? ...\n"
        "• Session ID: sess_3ba056c2-651f-4150-bebb-40b87b75db1e\n"
        "  Resume with: kiro-cli --resume-id sess_3ba056c2-651f-4150-bebb-40b87b75db1e\n"
    )
    assert j.parse_session_id(banner) == "sess_3ba056c2-651f-4150-bebb-40b87b75db1e"


def test_parse_session_id_none_when_absent():
    assert j.parse_session_id("no id here") is None
    assert j.parse_session_id("") is None


# --- meta shape -------------------------------------------------------------

def test_build_meta_shape():
    sid = "sess_11111111-2222-3333-4444-555555555555"
    meta = j.build_meta("my-journey", goal="My Journey", king_session=sid)
    assert meta["journey_id"] == "my-journey"
    assert meta["tui"] == "ENABLED"
    assert meta["king_session"] == sid
    assert meta["king_resume"] == f"kiro-cli --v3 chat --resume-id {sid}"
    assert meta["status"] == "active"
    assert meta["tree_root"] == {"role": "king", "level": 1, "session_id": sid}
    assert meta["knights"] == []
    assert "created_at" in meta and "updated_at" in meta
    assert "parent = who COMMANDS" in meta["parenting_rule"]


# --- create_journey: happy path (spawn result resolves the id) --------------

def _fakes(spawn_result, existing_dirs=False, existing_session=False):
    calls = {"spawned": [], "killed": [], "captured": [], "scanned": 0}

    def spawn(agent, tmux_session=None, cwd=None):
        calls["spawned"].append({"agent": agent, "tmux": tmux_session, "cwd": cwd})
        return spawn_result

    def session_exists(name):
        return existing_session

    def capture_pane(name):
        calls["captured"].append(name)
        return ""  # no banner by default; forces spawn-result path

    def scan_knights():
        calls["scanned"] += 1
        return {"knights": []}

    def kill_session(name):
        calls["killed"].append(name)

    return spawn, session_exists, capture_pane, scan_knights, kill_session, calls


def test_create_journey_happy_path(tmp_path):
    sid = "sess_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    spawn, se, cap, scan, kill, calls = _fakes(
        {"tmux_session": "my-journey", "session_id": sid, "resolved": True}
    )
    out = j.create_journey("My Journey", tmp_path,
                           spawn=spawn, session_exists=se, capture_pane=cap,
                           scan_knights=scan, kill_session=kill)
    assert out == {"journey_id": "my-journey", "tmux_session": "my-journey",
                   "king_session": sid}
    # spawn was at ROOT_DIR, session named after the journey (NOT king-<slug>).
    assert calls["spawned"][0]["tmux"] == "my-journey"
    assert calls["spawned"][0]["cwd"] == str(tmp_path)
    # dirs + atomic meta written.
    meta_path = tmp_path / "journeys" / "my-journey" / "meta.json"
    assert (tmp_path / "journeys" / "my-journey" / "artifacts").is_dir()
    doc = json.loads(meta_path.read_text())
    assert doc["king_session"] == sid
    assert doc["tree_root"]["session_id"] == sid
    assert calls["killed"] == []  # nothing to clean up


# --- collision rejects (create nothing) -------------------------------------

def test_reject_when_journey_dir_exists(tmp_path):
    (tmp_path / "journeys" / "dup").mkdir(parents=True)
    spawn, se, cap, scan, kill, calls = _fakes({})
    with pytest.raises(j.JourneyError) as ei:
        j.create_journey("dup", tmp_path, spawn=spawn, session_exists=se,
                         capture_pane=cap, scan_knights=scan, kill_session=kill)
    assert "already exists" in str(ei.value)
    assert calls["spawned"] == []  # never spawned


def test_reject_when_tmux_session_exists(tmp_path):
    spawn, se, cap, scan, kill, calls = _fakes({}, existing_session=True)
    with pytest.raises(j.JourneyError) as ei:
        j.create_journey("taken", tmp_path, spawn=spawn, session_exists=se,
                         capture_pane=cap, scan_knights=scan, kill_session=kill)
    assert "already exists" in str(ei.value)
    assert calls["spawned"] == []
    assert not (tmp_path / "journeys" / "taken").exists()  # nothing created


# --- id capture fallbacks ---------------------------------------------------

def test_capture_falls_back_to_banner(tmp_path):
    sid = "sess_12345678-90ab-cdef-1234-567890abcdef"
    # spawn did NOT resolve -> must parse the pane banner.
    def spawn(agent, tmux_session=None, cwd=None):
        return {"tmux_session": tmux_session, "session_id": None, "resolved": False}

    def capture_pane(name):
        return f"• Session ID: {sid}\n  Resume with: kiro-cli --resume-id {sid}\n"

    out = j.create_journey("banner-j", tmp_path, spawn=spawn,
                           session_exists=lambda n: False,
                           capture_pane=capture_pane,
                           scan_knights=lambda: {"knights": []},
                           kill_session=lambda n: None)
    assert out["king_session"] == sid


def test_capture_falls_back_to_scan(tmp_path):
    sid = "sess_99999999-8888-7777-6666-555555555555"
    def spawn(agent, tmux_session=None, cwd=None):
        return {"tmux_session": tmux_session, "session_id": None, "resolved": False}

    def scan():
        return {"knights": [{"tmux_session": "scan-j", "session_id": sid}]}

    out = j.create_journey("scan-j", tmp_path, spawn=spawn,
                           session_exists=lambda n: False,
                           capture_pane=lambda n: "",   # no banner
                           scan_knights=scan,
                           kill_session=lambda n: None)
    assert out["king_session"] == sid


# --- cleanup on failure (no orphans) ----------------------------------------

def test_cleanup_when_id_unresolvable(tmp_path):
    killed = []
    def spawn(agent, tmux_session=None, cwd=None):
        return {"tmux_session": tmux_session, "session_id": None, "resolved": False}

    with pytest.raises(j.JourneyError) as ei:
        j.create_journey("orphan-j", tmp_path, spawn=spawn,
                         session_exists=lambda n: False,
                         capture_pane=lambda n: "",            # no banner
                         scan_knights=lambda: {"knights": []},  # no match
                         kill_session=lambda n: killed.append(n))
    assert "session id" in str(ei.value)
    # the spawned tmux was killed AND the journey dir removed — no orphans.
    assert killed == ["orphan-j"]
    assert not (tmp_path / "journeys" / "orphan-j").exists()


def test_cleanup_when_spawn_raises(tmp_path):
    killed = []
    def spawn(agent, tmux_session=None, cwd=None):
        raise RuntimeError("spawn boom")

    with pytest.raises(j.JourneyError) as ei:
        j.create_journey("boom-j", tmp_path, spawn=spawn,
                         session_exists=lambda n: False,
                         capture_pane=lambda n: "",
                         scan_knights=lambda: {"knights": []},
                         kill_session=lambda n: killed.append(n))
    assert "spawn boom" in str(ei.value)
    # spawn raised before we marked it spawned -> no kill, but dir cleaned.
    assert not (tmp_path / "journeys" / "boom-j").exists()
