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
    def __init__(self, boot_session="jtw-console", **kw):
        self.started = False
        self.switched = []
        self.boot_session = boot_session
        self.ensured = []          # names passed to ensure_console_session
        self.existing = set()      # sessions FakeTtyd reports as live

    def is_running(self):
        return self.started

    def start(self):
        self.started = True

    def stop(self, timeout: float = 5.0):
        self.started = False

    def session_exists(self, name):
        return name in self.existing

    def ensure_console_session(self, name, cwd):
        self.ensured.append(name)
        self.existing.add(name)
        return True

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
    # Neutralize any JTW_BOOT_SESSION inherited from the host env so the shared
    # client fixture always takes the default (console) boot path.
    monkeypatch.setattr(app_mod, "BOOT_SESSION_ENV", None)
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
    # Pin the public-host wiring so the test doesn't depend on ambient env.
    monkeypatch.setattr(app_mod, "PUBLIC_HOST", "127.0.0.1")
    monkeypatch.setattr(app_mod, "TTYD_SCHEME", "http")
    monkeypatch.setattr(app_mod, "PUBLIC_PORT", "7681")
    monkeypatch.setattr(app_mod, "TTYD_CRED", None)
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


# --- boot path: knight-independent, always boots the console session --------

def _mk_fake_and_patch(monkeypatch):
    """Patch TtydManager with a FakeTtyd and return the instance (for boot
    tests that must inspect lifespan behavior)."""
    fake = FakeTtyd()
    monkeypatch.setattr(app_mod, "TtydManager", lambda **kw: fake)
    return fake


def test_boot_defaults_to_console_session_no_knight(monkeypatch):
    """No JTW_BOOT_SESSION + no live knight -> ttyd boots the auto-created
    console session, never a knight. ttyd ALWAYS starts."""
    monkeypatch.setattr(app_mod, "BOOT_SESSION_ENV", None)
    monkeypatch.setattr(app_mod, "CONSOLE_SESSION", "jtw-console")
    fake = _mk_fake_and_patch(monkeypatch)

    with TestClient(app_mod.app) as tc:
        assert fake.started is True                 # ttyd always starts
        assert fake.ensured == ["jtw-console"]      # console auto-created
        assert fake.boot_session == "jtw-console"   # booted to the console
        assert tc.get("/api/health").json()["ttyd_up"] is True


def test_boot_honors_live_override(monkeypatch):
    """JTW_BOOT_SESSION set AND live -> ttyd boots that session (escape hatch)."""
    monkeypatch.setattr(app_mod, "BOOT_SESSION_ENV", "steward-tui")
    monkeypatch.setattr(app_mod, "CONSOLE_SESSION", "jtw-console")
    fake = _mk_fake_and_patch(monkeypatch)
    fake.existing.add("steward-tui")                # override is live

    with TestClient(app_mod.app):
        assert fake.started is True
        assert fake.boot_session == "steward-tui"   # honored the override
        assert fake.ensured == []                   # did NOT touch the console


def test_boot_dead_override_fails_loud(monkeypatch):
    """JTW_BOOT_SESSION set but NOT live -> startup fails loud (no silent
    fallback to a different session)."""
    monkeypatch.setattr(app_mod, "BOOT_SESSION_ENV", "ghost-tui")
    monkeypatch.setattr(app_mod, "CONSOLE_SESSION", "jtw-console")
    fake = _mk_fake_and_patch(monkeypatch)
    # fake.existing stays empty -> ghost-tui is not live

    raised = False
    try:
        with TestClient(app_mod.app):
            pass
    except RuntimeError:
        raised = True
    assert raised is True
    assert fake.started is False                    # never started on a dead override


# --- POST /api/journey ------------------------------------------------------

def test_new_journey_success(client, monkeypatch, tmp_path):
    import journey as j
    sid = "sess_abcabcab-1111-2222-3333-444444444444"

    monkeypatch.setattr(app_mod, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "_import_motor_spawn",
                        lambda: (lambda agent, tmux_session=None, cwd=None: {
                            "tmux_session": tmux_session, "session_id": sid, "resolved": True}))
    monkeypatch.setattr(app_mod, "_tmux_session_exists", lambda n: False)
    monkeypatch.setattr(app_mod, "_tmux_capture_pane", lambda n: "")
    monkeypatch.setattr(app_mod, "_tmux_kill_session", lambda n: None)

    r = client.post("/api/journey", json={"name": "Smoke Test Journey"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["journey_id"] == "smoke-test-journey"
    assert body["tmux_session"] == "smoke-test-journey"
    assert body["king_session"] == sid
    # meta.json landed in the mocked ROOT_DIR
    assert (tmp_path / "journeys" / "smoke-test-journey" / "meta.json").is_file()


def test_new_journey_empty_name_400(client):
    r = client.post("/api/journey", json={"name": "   "})
    assert r.status_code == 400


def test_new_journey_collision_409(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "ROOT_DIR", tmp_path)
    (tmp_path / "journeys" / "dup-journey").mkdir(parents=True)
    # spawn must never be called on a collision
    called = {"spawn": False}
    monkeypatch.setattr(app_mod, "_import_motor_spawn",
                        lambda: (lambda *a, **k: called.__setitem__("spawn", True) or {}))
    monkeypatch.setattr(app_mod, "_tmux_session_exists", lambda n: False)

    r = client.post("/api/journey", json={"name": "Dup Journey"})
    assert r.status_code == 409
    assert called["spawn"] is False
