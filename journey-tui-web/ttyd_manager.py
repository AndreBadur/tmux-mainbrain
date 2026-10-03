"""The ONE ttyd child + the tmux switch-client driver.

Design (locked, round-6 sketch; risk-#1 proven in the lab 2026-10-03):

* A single long-lived ``ttyd`` process runs ``tmux attach -t <boot_session>``.
  The browser's ``<iframe>`` points at this one port and NEVER reloads.
* Switching which knight the iframe shows is NOT a new attach — it is
  ``tmux switch-client -c <ttyd_client> -t <target>``, which re-points ttyd's
  OWN tmux client underneath the still-open websocket. PROVEN: the ttyd client
  (its pty/tty) is distinct from any human client, and ``-c <client>`` is
  REQUIRED — a bare ``switch-client`` would act on the calling shell, not ttyd.

Defensive discipline:
* The child is owned here and only started from the app lifespan (never at
  import time) so tests and imports never spawn a real ttyd.
* ``start`` is idempotent; ``stop`` is terminate -> BOUNDED wait -> kill ->
  reap (no zombies, no unbounded hang).
* Every ``subprocess.run`` carries an explicit ``timeout=``.
* We NEVER kill-session / kill-server — only ever switch-client.
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional

from models import SwitchResult

# v1 binds localhost only. The dev may flip this to "0.0.0.0" for LAN exposure
# — that is their accepted security call (solo/isolated user).
DEFAULT_BIND = "127.0.0.1"

# Bounded timeouts (seconds) for the short tmux commands.
TMUX_TIMEOUT = 5.0
# ttyd lazily spawns its `tmux attach` child only on the first websocket
# connection, so the client may not exist until a browser connects. We retry
# resolving it for a short, bounded window on demand.
CLIENT_RESOLVE_RETRIES = 3
CLIENT_RESOLVE_DELAY = 0.3


class TtydError(RuntimeError):
    """Raised when the ttyd child cannot be managed as required."""


class TtydManager:
    """Owns the single ttyd child and drives tmux switch-client."""

    def __init__(
        self,
        boot_session: str,
        port: int = 7681,
        bind: str = DEFAULT_BIND,
        ttyd_bin: str = "ttyd",
        tmux_bin: str = "tmux",
        credential: Optional[str] = None,
    ) -> None:
        self.boot_session = boot_session
        self.port = port
        self.bind = bind
        self.ttyd_bin = ttyd_bin
        self.tmux_bin = tmux_bin
        # ttyd basic-auth, "user:pass". When set, ttyd is launched with
        # ``-c user:pass`` so every terminal connection must authenticate. Kept
        # ONLY in memory on this manager; never logged (see _safe_cmd).
        self.credential = credential or None
        self._proc: Optional[subprocess.Popen] = None
        # ttyd's tmux client name (its pty), cached once resolved. After a
        # switch the client's SESSION changes, so the boot-session heuristic
        # stops finding it — we must track it by name from the first resolve.
        self._client: Optional[str] = None

    # --- lifecycle ---------------------------------------------------------

    def _build_cmd(self) -> list[str]:
        """Assemble the ttyd argv. Basic-auth (``-c user:pass``) is included
        when a credential is configured. Binding ``0.0.0.0`` exposes the
        terminal on the LAN — that is the operator's explicit call via
        ``JTW_TTYD_BIND`` and should be paired with a credential.
        """
        cmd = [
            self.ttyd_bin,
            "-p", str(self.port),
            "-i", self.bind,
            "-W",  # writable terminal
            "-t", "disableLeaveAlert=true",
        ]
        if self.credential:
            cmd += ["-c", self.credential]
        cmd += [self.tmux_bin, "attach", "-t", self.boot_session]
        return cmd

    def _safe_cmd(self, cmd: list[str]) -> list[str]:
        """A copy of ``cmd`` with the basic-auth credential redacted, safe to
        log / surface in errors (never leak user:pass)."""
        redacted = list(cmd)
        for i, tok in enumerate(redacted):
            if tok == "-c" and i + 1 < len(redacted):
                redacted[i + 1] = "***:***"
        return redacted

    def start(self) -> None:
        """Launch the ONE ttyd child. Idempotent: a no-op if already running."""
        if self.is_running():
            return
        cmd = self._build_cmd()
        try:
            self._proc = subprocess.Popen(cmd)  # noqa: S603 - fixed argv, no shell
        except FileNotFoundError as exc:
            raise TtydError(
                f"ttyd binary not found: {self.ttyd_bin!r} (is ttyd on PATH?)"
            ) from exc

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate -> bounded wait -> kill -> reap. Always safe to call."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass  # last resort; the OS will reap the orphan
        self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # --- tmux driving ------------------------------------------------------

    def _run_tmux(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            [self.tmux_bin, *args],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT,
        )

    def resolve_ttyd_client(self) -> Optional[str]:
        """Return ttyd's tmux client name (its pty), or None if not yet attached.

        ttyd lazily spawns ``tmux attach -t <boot_session>`` on the first
        websocket connection, so at startup the client is the one attached to
        our boot session. We cache that name: after a switch the client's
        session changes, so re-matching by boot session would fail — the cached
        name is authoritative thereafter. We verify the cached client still
        exists; if it vanished (browser closed), we re-resolve by boot session.
        """
        for _ in range(CLIENT_RESOLVE_RETRIES):
            res = self._run_tmux([
                "list-clients", "-F",
                "#{client_name}\t#{client_session}\t#{client_tty}",
            ])
            if res.returncode == 0:
                names = []
                boot_client = None
                for line in res.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) != 3:
                        continue
                    name, session, _tty = parts
                    names.append(name)
                    if session == self.boot_session:
                        boot_client = name
                # Prefer a still-present cached client (survives switches).
                if self._client and self._client in names:
                    return self._client
                if boot_client is not None:
                    self._client = boot_client
                    return boot_client
            time.sleep(CLIENT_RESOLVE_DELAY)
        return None

    def switch_client(self, target: str, client: Optional[str] = None) -> SwitchResult:
        """Re-point ttyd's tmux client to ``target`` via switch-client.

        ``-c <client>`` is REQUIRED (proven): without it switch-client acts on
        the calling shell, not ttyd's client.
        """
        resolved_client = client or self.resolve_ttyd_client()
        if not resolved_client:
            return SwitchResult(
                ok=False,
                target=target,
                error="could not resolve ttyd's tmux client "
                      "(has a browser connected to the terminal yet?)",
            )
        try:
            res = self._run_tmux(
                ["switch-client", "-c", resolved_client, "-t", target]
            )
        except subprocess.TimeoutExpired:
            return SwitchResult(ok=False, target=target,
                                error="tmux switch-client timed out")
        if res.returncode != 0:
            return SwitchResult(
                ok=False, target=target,
                error=res.stderr.strip() or "switch-client failed",
            )
        self._client = resolved_client
        return SwitchResult(ok=True, target=target)

    # --- console session ---------------------------------------------------

    def session_exists(self, name: str) -> bool:
        """Whether a tmux session named ``name`` exists right now."""
        res = self._run_tmux(["has-session", "-t", name])
        return res.returncode == 0

    def ensure_console_session(self, name: str, cwd: str) -> bool:
        """Idempotently create a plain-shell tmux session for direct use.

        Returns True if the session exists (already, or after creation). This is
        a NORMAL interactive shell — NOT a kiro-cli knight — so you can type
        arbitrary commands. We never kill it here; it lives until the operator
        ends it. Creation is bounded by the tmux timeout; a creation failure is
        surfaced to the caller (False), never swallowed silently.
        """
        if self.session_exists(name):
            return True
        res = self._run_tmux([
            "new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd,
        ])
        if res.returncode != 0:
            return False
        return self.session_exists(name)
