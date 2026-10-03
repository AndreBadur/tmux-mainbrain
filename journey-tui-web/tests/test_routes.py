"""Route tests via TestClient. The motor scan and ttyd are stubbed so no real
process is ever spawned and no motor import is required."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_mod  # noqa: E402
from models import SwitchResult  # noqa: E402


SAMPLE_TREES_META = {
    "journey_id": "j-test",
    "tree_root": {"session_id": "sess_king"},
    "knights": [
        {"role": "steward", "session_id": "sess_live", "tmux_session": "steward-tui",
         "parent": "sess_king", "level": 2},
        {"role": "coder", "session_id": "sess_dead", "tmux_session": "coder-tui",
         "parent": "sess_king", "level": 2},
        {"role": "ghost", "session_id": "sess_notmux", "parent": "sess_king", "level": 2},
    ],
}

SAMPLE_LIVE = {
    "sess_live": {"alive": True, "context_pct": 10.0},
    "sess_dead": {"alive": False, "context_pct": 20.0},
}


class FakeTtyd:
    def __init__(self):
        self.started = False
        self.switched = []

    def is_running(self):
        return self.started

    def start(self):
        self.started = True

    def stop(self, timeout: float = 5.0):
        self.started = False

    def switch_client(self, target, client=None):
        self.switched.append(target)
        return SwitchResult(ok=True, target=target)


@pytest.fixture()
def client(monkeypatch):
    import cascade as c

    # Stub the motor scan so load_cascade needs no real motor.
    def fake_scan(root=None):
        return {"count": 2, "knights": [
            {"session_id": sid, **vals} for sid, vals in SAMPLE_LIVE.items()
        ]}

    monkeypatch.setattr(app_mod, "_import_motor_scan", lambda: fake_scan)
    monkeypatch.setattr(c, "read_meta_files", lambda root: ([SAMPLE_TREES_META], []))
    # Stub live tmux sessions: only steward-tui is "running" so sess_live is
    # clickable; sess_dead (coder-tui) and sess_notmux are revivable.
    monkeypatch.setattr(
        c, "list_live_tmux_sessions", lambda tmux_bin="tmux": frozenset({"steward-tui"})
    )

    # Stub the motor's resume_clean so revive never spawns a real tmux/kiro.
    revive_calls = []

    def fake_resume_clean(session_id, tmux_session=None, cwd=None, **kw):
        revive_calls.append({"session_id": session_id, "tmux_session": tmux_session})
        return {"tmux_session": tmux_session, "session_id": session_id, "resolved": True}

    monkeypatch.setattr(app_mod, "_import_motor_resume_clean", lambda: fake_resume_clean)

    # Replace TtydManager with the fake so lifespan starts no real ttyd.
    fake = FakeTtyd()
    monkeypatch.setattr(app_mod, "TtydManager", lambda **kw: fake)

    with TestClient(app_mod.app) as tc:
        tc.fake_ttyd = fake
        tc.revive_calls = revive_calls
        yield tc


def test_health_reports_ttyd_up(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ttyd_up"] is True


def test_config_reports_terminal_url(client, monkeypatch):
    # Defaults: localhost, no auth.
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["terminal_url"] == "http://127.0.0.1:7681/"
    assert body["auth_required"] is False


def test_cascade_returns_tree_with_action_flags(client):
    r = client.get("/api/cascade")
    assert r.status_code == 200
    trees = r.json()
    assert len(trees) == 1
    root = trees[0]["root"]
    kids = {k["session_id"]: k for k in root["children"]}
    assert kids["sess_live"]["clickable"] is True
    assert kids["sess_live"]["revivable"] is False
    assert kids["sess_dead"]["clickable"] is False     # coder-tui not live
    assert kids["sess_dead"]["revivable"] is True       # -> revive on click
    assert kids["sess_notmux"]["clickable"] is False    # no tmux
    assert kids["sess_notmux"]["revivable"] is True      # has session id


def test_switch_live_knight_ok(client):
    r = client.post("/api/switch/sess_live")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["target"] == "steward-tui"
    assert client.fake_ttyd.switched == ["steward-tui"]
    assert client.revive_calls == []  # a live knight is never revived


def test_switch_unknown_sid_404(client):
    r = client.post("/api/switch/sess_nope")
    assert r.status_code == 404


def test_switch_dead_knight_revives_then_switches(client):
    # sess_dead -> coder-tui is not live, so the route resume_cleans it, then
    # switches ttyd to the revived session.
    r = client.post("/api/switch/sess_dead")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["target"] == "coder-tui"
    assert client.revive_calls == [
        {"session_id": "sess_dead", "tmux_session": "coder-tui"}
    ]
    assert client.fake_ttyd.switched == ["coder-tui"]


def test_switch_knight_without_tmux_revives_with_derived_name(client):
    # sess_notmux has no stored tmux_session; the route derives one
    # (<role>-<idsuffix>) and revives there. session_id has no sess_ prefix /
    # hex suffix, so the derived name falls back to the role.
    r = client.post("/api/switch/sess_notmux")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert len(client.revive_calls) == 1
    assert client.revive_calls[0]["session_id"] == "sess_notmux"
    # ttyd switched to whatever name resume_clean returned.
    assert client.fake_ttyd.switched == [body["target"]]
