//! The interactive WATCH view — an embedded pty running `tmux attach` to a live
//! knight (pipeline D3/D4), built on the shared [`PtySession`] component.
//!
//! `WatchSession` = a `PtySession` running `tmux attach [-r] -t <session>` plus
//! the tmux-specific CLOSE = DETACH behavior (D4): on drop we `tmux
//! detach-client -s <session>` (kills only the CLIENT, the session survives)
//! BEFORE dropping the inner pty. We NEVER kill-session / kill-server.

use anyhow::Result;
use portable_pty::CommandBuilder;

use crate::pty::PtySession;

/// A running watch session bound to one tmux session name.
pub struct WatchSession {
    /// The tmux session we attached to (for the detach-on-close).
    pub tmux_session: String,
    /// Read-only attach (`-r`) omits the writer.
    pub read_only: bool,
    pty: PtySession,
}

impl WatchSession {
    /// Open an interactive (or read-only) attach to `tmux_session` sized to
    /// `rows`x`cols`. `read_only` uses `tmux attach -r` and omits the writer.
    pub fn open(tmux_session: &str, rows: u16, cols: u16, read_only: bool) -> Result<Self> {
        let mut cmd = CommandBuilder::new("tmux");
        cmd.arg("attach");
        if read_only {
            cmd.arg("-r");
        }
        cmd.arg("-t");
        cmd.arg(tmux_session);
        cmd.env("TERM", "xterm-256color");

        let pty = PtySession::spawn(cmd, rows, cols, read_only, tmux_session)?;
        Ok(Self {
            tmux_session: tmux_session.to_string(),
            read_only,
            pty,
        })
    }

    pub fn parser(&self) -> std::sync::MutexGuard<'_, vt100::Parser> {
        self.pty.parser()
    }

    pub fn take_dirty(&self) -> bool {
        self.pty.take_dirty()
    }

    pub fn has_exited(&mut self) -> bool {
        self.pty.has_exited()
    }

    pub fn resize(&mut self, rows: u16, cols: u16) {
        self.pty.resize(rows, cols);
    }

    pub fn write_input(&mut self, bytes: &[u8]) {
        self.pty.write_input(bytes);
    }

    pub fn apply_scrollback(&mut self) {
        self.pty.apply_scrollback();
    }

    pub fn scrollback_offset(&self) -> usize {
        self.pty.scrollback_offset()
    }

    pub fn scroll_history(&mut self, delta: i32) {
        self.pty.scroll_history(delta);
    }

    pub fn snap_to_live(&mut self) -> bool {
        self.pty.snap_to_live()
    }

    pub fn on_alternate_screen(&self) -> bool {
        self.pty.on_alternate_screen()
    }
}

impl Drop for WatchSession {
    fn drop(&mut self) {
        // DETACH the client cleanly (D4) BEFORE the inner pty drops — kills only
        // the attach client; the session + its processes survive. NEVER
        // kill-session / kill-server.
        let _ = std::process::Command::new("tmux")
            .arg("detach-client")
            .arg("-s")
            .arg(&self.tmux_session)
            .output();
        // If the attach client is still alive after detach, kill THAT process
        // (not the session). The inner PtySession::drop then reaps it.
        if self.pty.child_alive() {
            self.pty.kill_child();
        }
        // self.pty drops here → reader joined, child reaped (no zombies).
    }
}
