//! `PtySession` — the ONE reusable embedded-pty component (the claudetui /
//! ratatui-ghostty blueprint), shared by the WATCH tab (`tmux attach`) and the
//! EDIT view (`$EDITOR <file>`).
//!
//! * `portable_pty` master spawns an arbitrary [`CommandBuilder`] in a slave pty.
//! * A background reader thread drains the master and feeds a shared
//!   `vt100::Parser` (behind a `Mutex`); the UI renders `Parser::screen()`
//!   cells → ratatui spans off the reader thread, with a dirty flag driving the
//!   ≤30fps throttle.
//! * Input: focused key/mouse events are encoded and written via
//!   `take_writer()` (omitted when `read_only`).
//! * Resize: `MasterPty::resize(PtySize{rows,cols,..})`.
//! * Drop: the child is force-killed if still running, then `wait()`-reaped
//!   (no zombies), and the reader thread joined. Command-specific teardown
//!   (e.g. tmux `detach-client`) is layered by the wrappers BEFORE they drop
//!   the inner `PtySession`.

use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use anyhow::{Context, Result};
use portable_pty::{Child, ChildKiller, CommandBuilder, MasterPty, PtySize};

/// vt100 scrollback history retained per pty (lines). Generous so a long
/// kiro-cli/tmux session's earlier output stays readable after it scrolls off.
pub const SCROLLBACK_LINES: usize = 5000;

// Compile-time guarantee we never regress to the zero-scrollback bug.
const _: () = assert!(SCROLLBACK_LINES > 0, "pty scrollback must be non-zero");

/// A generic interactive pty hosting one child process, rendered into a MAIN
/// sub-rect. Command-agnostic: the caller supplies the [`CommandBuilder`].
pub struct PtySession {
    parser: Arc<Mutex<vt100::Parser>>,
    writer: Option<Box<dyn Write + Send>>,
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
    killer: Box<dyn ChildKiller + Send + Sync>,
    stop: Arc<AtomicBool>,
    dirty: Arc<AtomicBool>,
    reader: Option<JoinHandle<()>>,
    last_size: (u16, u16),
    /// Current scrollback view offset (0 = live bottom; higher = further back).
    scrollback: usize,
}

impl PtySession {
    /// Spawn `cmd` in a fresh pty sized to `rows`x`cols`. When `read_only`, the
    /// writer is omitted (input is dropped). `label` names the reader thread.
    pub fn spawn(
        cmd: CommandBuilder,
        rows: u16,
        cols: u16,
        read_only: bool,
        label: &str,
    ) -> Result<Self> {
        let (rows, cols) = sane_size(rows, cols);
        let pty_system = portable_pty::native_pty_system();
        let pair = pty_system
            .openpty(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .context("openpty failed")?;

        let child = pair
            .slave
            .spawn_command(cmd)
            .context("spawn command in pty failed")?;
        let killer = child.clone_killer();
        drop(pair.slave); // the child holds it now.

        let writer = if read_only {
            None
        } else {
            Some(pair.master.take_writer().context("take_writer failed")?)
        };

        let parser = Arc::new(Mutex::new(vt100::Parser::new(rows, cols, SCROLLBACK_LINES)));
        let stop = Arc::new(AtomicBool::new(false));
        let dirty = Arc::new(AtomicBool::new(true));

        let mut reader_pipe = pair
            .master
            .try_clone_reader()
            .context("try_clone_reader failed")?;
        let r_parser = Arc::clone(&parser);
        let r_stop = Arc::clone(&stop);
        let r_dirty = Arc::clone(&dirty);
        let reader = std::thread::Builder::new()
            .name(format!("pty-reader-{label}"))
            .spawn(move || {
                let mut buf = [0u8; 8192];
                loop {
                    if r_stop.load(Ordering::Relaxed) {
                        return;
                    }
                    match reader_pipe.read(&mut buf) {
                        Ok(0) => return, // EOF: child exited.
                        Ok(n) => {
                            if let Ok(mut p) = r_parser.lock() {
                                p.process(&buf[..n]);
                            }
                            r_dirty.store(true, Ordering::Relaxed);
                        }
                        Err(ref e) if e.kind() == std::io::ErrorKind::Interrupted => {}
                        Err(_) => return,
                    }
                }
            })
            .context("spawn reader thread failed")?;

        Ok(Self {
            parser,
            writer,
            master: pair.master,
            child,
            killer,
            stop,
            dirty,
            reader: Some(reader),
            last_size: (rows, cols),
            scrollback: 0,
        })
    }

    /// Lock and borrow the parser for rendering (recovers a poisoned lock so a
    /// panicked reader can't permanently blank the pane).
    pub fn parser(&self) -> std::sync::MutexGuard<'_, vt100::Parser> {
        self.parser.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Apply the current scrollback offset to the vt100 screen so the next
    /// render shows the historical rows, and RECONCILE our tracked offset with
    /// what vt100 actually applied (0 on an alt-screen / empty history). Called
    /// each frame before reading cells; takes `&mut self` so the counter can
    /// never drift into a phantom value (e.g. if the child switched to the
    /// alternate screen while we were scrolled back).
    pub fn apply_scrollback(&mut self) {
        let applied = {
            let mut p = self.parser.lock().unwrap_or_else(|e| e.into_inner());
            p.screen_mut().set_scrollback(self.scrollback);
            p.screen().scrollback()
        };
        self.scrollback = applied;
    }

    /// Current scrollback offset (0 = live). This is the REAL applied offset,
    /// kept in sync with vt100's clamp by [`Self::scroll_history`].
    pub fn scrollback_offset(&self) -> usize {
        self.scrollback
    }

    /// True if the watched pane is currently on the ALTERNATE screen (a
    /// full-screen TUI like kiro-cli / vim / less). Such panes keep NO
    /// scrollback — the app manages its own scrolling — so our overlay
    /// scrollback does not apply; the UI shows an honest hint instead.
    pub fn on_alternate_screen(&self) -> bool {
        let p = self.parser.lock().unwrap_or_else(|e| e.into_inner());
        p.screen().alternate_screen()
    }

    /// Scroll the history view by `delta` lines (positive = further back into
    /// history, negative = toward live). The REAL clamp is vt100's: we request
    /// the new offset via `set_scrollback` and read back `screen.scrollback()`
    /// — the actual achieved position (clamped to the real retained history, 0
    /// on an alt-screen). So the counter can never exceed what truly exists.
    /// Marks dirty only if the applied offset changed.
    pub fn scroll_history(&mut self, delta: i32) {
        let desired = (self.scrollback as i64 + delta as i64).clamp(0, SCROLLBACK_LINES as i64) as usize;
        let applied = {
            let mut p = self.parser.lock().unwrap_or_else(|e| e.into_inner());
            p.screen_mut().set_scrollback(desired);
            p.screen().scrollback() // the real clamped offset
        };
        if applied != self.scrollback {
            self.scrollback = applied;
            self.dirty.store(true, Ordering::Relaxed);
        }
    }

    /// Snap the view back to live (offset 0). Returns true if it changed.
    /// Called before forwarding real typing so the user never types while
    /// reviewing history.
    pub fn snap_to_live(&mut self) -> bool {
        if self.scrollback != 0 {
            self.scrollback = 0;
            let mut p = self.parser.lock().unwrap_or_else(|e| e.into_inner());
            p.screen_mut().set_scrollback(0);
            drop(p);
            self.dirty.store(true, Ordering::Relaxed);
            true
        } else {
            false
        }
    }

    /// Take and clear the dirty flag (drives the render throttle).
    pub fn take_dirty(&self) -> bool {
        self.dirty.swap(false, Ordering::Relaxed)
    }

    /// Whether the child has already exited (client detached / editor quit).
    pub fn has_exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)))
    }

    /// Resize the pty (and the vt100 grid) if the target size changed.
    pub fn resize(&mut self, rows: u16, cols: u16) {
        let (rows, cols) = sane_size(rows, cols);
        if (rows, cols) == self.last_size {
            return;
        }
        let _ = self.master.resize(PtySize {
            rows,
            cols,
            pixel_width: 0,
            pixel_height: 0,
        });
        if let Ok(mut p) = self.parser.lock() {
            p.screen_mut().set_size(rows, cols);
            // Preserve the scrollback view across resize.
            p.screen_mut().set_scrollback(self.scrollback);
        }
        self.last_size = (rows, cols);
        self.dirty.store(true, Ordering::Relaxed);
    }

    /// Forward raw bytes to the pty (encoded key/mouse input). No-op if
    /// read-only or the child is gone.
    pub fn write_input(&mut self, bytes: &[u8]) {
        if let Some(w) = self.writer.as_mut() {
            let _ = w.write_all(bytes);
            let _ = w.flush();
        }
    }

    /// Is the child still running?
    pub fn child_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Force-kill the child (used by wrappers whose teardown needs it).
    pub fn kill_child(&mut self) {
        let _ = self.killer.kill();
    }
}

impl Drop for PtySession {
    fn drop(&mut self) {
        // Stop the reader loop.
        self.stop.store(true, Ordering::Relaxed);
        // Kill the child if still running, then reap it (no zombies).
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = self.killer.kill();
        }
        let _ = self.child.wait();
        // Drop the writer to release the master's writer handle, then join the
        // reader (it sees EOF once the master drops after this scope).
        self.writer.take();
        if let Some(h) = self.reader.take() {
            let _ = h.join();
        }
    }
}

/// Clamp a pty size to a sane minimum (a 0-sized pty confuses tmux/vt100).
pub fn sane_size(rows: u16, cols: u16) -> (u16, u16) {
    (rows.max(1), cols.max(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sane_size_floors_at_one() {
        assert_eq!(sane_size(0, 0), (1, 1));
        assert_eq!(sane_size(24, 80), (24, 80));
    }

    #[test]
    fn spawns_and_reaps_a_real_child() {
        // A trivial, always-available command proves spawn → reader → drop/reap
        // without needing tmux or an editor.
        let cmd = CommandBuilder::new("true");
        let mut s = PtySession::spawn(cmd, 24, 80, false, "test").unwrap();
        // Give the child a moment to exit.
        std::thread::sleep(std::time::Duration::from_millis(200));
        assert!(s.has_exited(), "`true` should exit promptly");
        // Drop reaps cleanly (no panic, no hang).
        drop(s);
    }

    #[test]
    fn scrollback_self_clamps_to_real_history() {
        // A shell that emits NO output has an EMPTY scrollback. The fix: our
        // offset must self-clamp to what vt100 actually retains (0 here) —
        // never a phantom counter (the reported bug on the alt-screen King).
        let mut cmd = CommandBuilder::new("sh");
        cmd.arg("-c");
        cmd.arg("sleep 1");
        let mut s = PtySession::spawn(cmd, 24, 80, false, "scrolltest").unwrap();
        // Let the shell settle.
        std::thread::sleep(std::time::Duration::from_millis(150));

        assert_eq!(s.scrollback_offset(), 0, "starts at live");
        // Requesting history with no retained lines → stays 0 (self-clamped via
        // set_scrollback→scrollback() readback), NOT a phantom 200.
        s.scroll_history(200);
        assert_eq!(
            s.scrollback_offset(),
            0,
            "no history → offset self-clamps to 0 (no phantom counter)"
        );
        // Toward-live is a no-op at 0.
        s.scroll_history(-5);
        assert_eq!(s.scrollback_offset(), 0);
        // snap_to_live at 0 → no change.
        assert!(!s.snap_to_live());
        // apply_scrollback reconciles to the real applied offset (still 0).
        s.apply_scrollback();
        assert_eq!(s.scrollback_offset(), 0);

        drop(s);
    }

    #[test]
    fn scrollback_moves_with_real_history() {
        // A shell that prints many lines then lingers → vt100 retains real
        // scrollback, so our offset CAN advance (the non-alt-screen case that
        // must keep working). 24-row pane, 100 lines printed → history exists.
        let mut cmd = CommandBuilder::new("sh");
        cmd.arg("-c");
        cmd.arg("seq 1 100; sleep 2");
        let mut s = PtySession::spawn(cmd, 24, 80, false, "hist").unwrap();
        // Give the reader time to ingest the 100 lines.
        std::thread::sleep(std::time::Duration::from_millis(400));
        assert!(
            !s.on_alternate_screen(),
            "a plain shell is NOT on the alternate screen"
        );
        // Scroll back — with ~76 lines of history (100 - 24), the offset should
        // advance above 0 (self-clamped to real available).
        s.scroll_history(10);
        assert!(
            s.scrollback_offset() > 0,
            "real history → offset advances (got {})",
            s.scrollback_offset()
        );
        // Snap back to live.
        assert!(s.snap_to_live());
        assert_eq!(s.scrollback_offset(), 0);
        drop(s);
    }

    #[test]
    fn scroll_never_exceeds_configured_ceiling() {
        // Even before we know the real history, the requested value is capped at
        // SCROLLBACK_LINES; vt100 then clamps to actual on apply. This exercises
        // the request-side clamp without needing a history-producing child.
        let mut cmd = CommandBuilder::new("sh");
        cmd.arg("-c");
        cmd.arg("sleep 1");
        let mut s = PtySession::spawn(cmd, 24, 80, false, "ceil").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(100));
        s.scroll_history(i32::MAX);
        assert!(
            s.scrollback_offset() <= SCROLLBACK_LINES,
            "offset never exceeds the retained ceiling"
        );
        drop(s);
    }
}
