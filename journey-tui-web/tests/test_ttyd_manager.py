"""Unit tests for ttyd_manager argv assembly + credential redaction.

Pure: these never spawn a real ttyd — they inspect the argv the manager would
launch and the redaction used for logging.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ttyd_manager import TtydManager  # noqa: E402


def test_cmd_without_credential_has_no_auth_flag():
    m = TtydManager(boot_session="steward-tui", port=7681, bind="127.0.0.1")
    cmd = m._build_cmd()
    assert "-c" not in cmd
    assert cmd[:7] == [
        "ttyd", "-p", "7681", "-i", "127.0.0.1", "-W", "-t",
    ]
    # ends with the attach to the boot session
    assert cmd[-4:] == ["tmux", "attach", "-t", "steward-tui"]


def test_cmd_with_credential_includes_auth_flag():
    m = TtydManager(
        boot_session="steward-tui", port=9000, bind="0.0.0.0",
        credential="alice:s3cret",
    )
    cmd = m._build_cmd()
    assert "-c" in cmd
    i = cmd.index("-c")
    assert cmd[i + 1] == "alice:s3cret"
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "0.0.0.0"


def test_safe_cmd_redacts_credential():
    m = TtydManager(
        boot_session="steward-tui", credential="alice:s3cret",
    )
    cmd = m._build_cmd()
    safe = m._safe_cmd(cmd)
    # the real credential must NOT appear anywhere in the redacted argv
    assert "alice:s3cret" not in safe
    i = safe.index("-c")
    assert safe[i + 1] == "***:***"
    # non-credential tokens are preserved
    assert safe[0] == "ttyd"


def test_empty_credential_is_treated_as_none():
    m = TtydManager(boot_session="s", credential="")
    assert m.credential is None
    assert "-c" not in m._build_cmd()


# --- ensure_console_session: idempotent create ------------------------------

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ensure_console_session_creates_when_missing(monkeypatch):
    m = TtydManager(boot_session="jtw-console")
    calls = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "has-session":
            # first probe: not present (rc=1); after new-session: present (rc=0)
            created = any(a[0] == "new-session" for a in calls[:-1])
            return _FakeProc(returncode=0 if created else 1)
        if args[0] == "new-session":
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(m, "_run_tmux", fake_run)
    assert m.ensure_console_session("jtw-console", cwd="/tmp") is True
    assert any(a[0] == "new-session" for a in calls), "should create when missing"


def test_ensure_console_session_idempotent_when_exists(monkeypatch):
    m = TtydManager(boot_session="jtw-console")
    calls = []

    def fake_run(args):
        calls.append(args)
        if args[0] == "has-session":
            return _FakeProc(returncode=0)  # already exists
        return _FakeProc(returncode=0)

    monkeypatch.setattr(m, "_run_tmux", fake_run)
    assert m.ensure_console_session("jtw-console", cwd="/tmp") is True
    # idempotent: existing session -> NO new-session call, no error
    assert all(a[0] != "new-session" for a in calls)


def test_ensure_console_session_reports_failure(monkeypatch):
    m = TtydManager(boot_session="jtw-console")

    def fake_run(args):
        if args[0] == "has-session":
            return _FakeProc(returncode=1)       # not present
        if args[0] == "new-session":
            return _FakeProc(returncode=1, stderr="boom")  # create failed
        return _FakeProc(returncode=1)

    monkeypatch.setattr(m, "_run_tmux", fake_run)
    # fail loud: creation failure returns False, never raises/hangs
    assert m.ensure_console_session("jtw-console", cwd="/tmp") is False
