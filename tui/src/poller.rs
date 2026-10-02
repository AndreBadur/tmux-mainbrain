//! Background workers — non-blocking data feeds to the UI over a channel.
//!
//! * The **index poller** runs `scan_knights` on an interval (live state).
//! * The **cascade loader** re-reads `journeys/*/meta.json` on the same tick
//!   (the governed command tree; cheap local file reads).
//!
//! Both run on one worker thread and push [`PollMsg`] to the UI. The UI thread
//! never blocks on the motor or the filesystem. Clean shutdown: the worker uses
//! `recv_timeout` on a stop channel, so it wakes promptly on quit and never
//! busy-waits.

use std::path::PathBuf;
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::thread::JoinHandle;
use std::time::Duration;

use crate::cascade::{self, Cascade};
use crate::motor::{Knight, MotorClient};
use crate::runtime::LiveTmux;

/// A message from the worker to the UI.
#[derive(Debug)]
pub enum PollMsg {
    /// A fresh live index (scan_knights) — feeds context%/purpose ONLY.
    Knights(Vec<Knight>),
    /// The set of currently-alive tmux session names — the AUTHORITY for
    /// runtime presence (the ●/○ glyph and the attach-vs-revive decision).
    Tmux(LiveTmux),
    /// A freshly-loaded governed cascade (journeys/*/meta.json).
    Cascade(Cascade),
    /// A non-fatal warning for the status bar (partial failure).
    Warn(String),
    /// The index poll failed; carries a reason for the status bar.
    Error(String),
}

/// Handle to the running worker. Dropping (or [`PollerHandle::stop`]) signals
/// the thread to exit and joins it. [`PollerHandle::poke`] forces an immediate
/// poll (used by the forced-reload path) without waiting for the tick.
pub struct PollerHandle {
    ctrl_tx: Option<Sender<Ctrl>>,
    handle: Option<JoinHandle<()>>,
}

/// Control messages to the worker.
enum Ctrl {
    /// Poll now, don't wait for the interval.
    PollNow,
}

impl PollerHandle {
    /// Spawn the worker. Polls immediately, then every `interval`.
    pub fn spawn(
        client: MotorClient,
        root_dir: PathBuf,
        interval: Duration,
        out: Sender<PollMsg>,
    ) -> Self {
        let (ctrl_tx, ctrl_rx) = std::sync::mpsc::channel::<Ctrl>();
        let handle = std::thread::Builder::new()
            .name("motor-poller".to_string())
            .spawn(move || run(client, root_dir, interval, out, ctrl_rx))
            .expect("failed to spawn motor-poller thread");
        Self {
            ctrl_tx: Some(ctrl_tx),
            handle: Some(handle),
        }
    }

    /// Force an immediate poll cycle (folder reload triggers this so the
    /// cascade refreshes now instead of up to `interval` later). Non-blocking.
    pub fn poke(&self) {
        if let Some(tx) = &self.ctrl_tx {
            let _ = tx.send(Ctrl::PollNow);
        }
    }

    /// Signal the worker to stop and join it. Idempotent.
    pub fn stop(&mut self) {
        // Dropping the control sender is the stop signal (Disconnected).
        self.ctrl_tx.take();
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

impl Drop for PollerHandle {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run(
    client: MotorClient,
    root_dir: PathBuf,
    interval: Duration,
    out: Sender<PollMsg>,
    ctrl_rx: Receiver<Ctrl>,
) {
    loop {
        // 1) Governed cascade from meta.json (local reads; cheap).
        let (casc, errors) = cascade::load_all(&root_dir);
        if out.send(PollMsg::Cascade(casc)).is_err() {
            return;
        }
        if !errors.is_empty() {
            let _ = out.send(PollMsg::Warn(format!(
                "meta.json: {} issue(s): {}",
                errors.len(),
                errors.join("; ")
            )));
        }

        // 2) Live tmux session set — ONE `tmux list-sessions` per cycle (never
        //    N forks). The AUTHORITY for runtime presence.
        if out.send(PollMsg::Tmux(list_live_tmux())).is_err() {
            return;
        }

        // 3) Live index from scan_knights (subprocess; may fail). Feeds ctx%.
        let msg = match client.scan_knights() {
            Ok(knights) => PollMsg::Knights(knights),
            Err(e) => PollMsg::Error(e.to_string()),
        };
        if out.send(msg).is_err() {
            return;
        }

        // Wait for the interval, but wake IMMEDIATELY on a `poke` (PollNow) so a
        // forced reload refreshes the cascade now. A dropped ctrl sender
        // (Disconnected) is the stop signal.
        match ctrl_rx.recv_timeout(interval) {
            Ok(Ctrl::PollNow) => { /* loop now */ }
            Err(RecvTimeoutError::Timeout) => { /* normal tick */ }
            Err(RecvTimeoutError::Disconnected) => return,
        }
    }
}

/// Run `tmux list-sessions -F '#{session_name}'` once and collect the names.
/// A tmux failure (e.g. no server running) yields an empty set — everything
/// then renders as revivable-offline, which is correct (no runtimes exist).
fn list_live_tmux() -> LiveTmux {
    let output = std::process::Command::new("tmux")
        .arg("list-sessions")
        .arg("-F")
        .arg("#{session_name}")
        .output();
    match output {
        Ok(o) if o.status.success() => {
            let text = String::from_utf8_lossy(&o.stdout);
            let names: Vec<String> = text
                .lines()
                .map(|l| l.trim().to_string())
                .filter(|l| !l.is_empty())
                .collect();
            LiveTmux::from_names(names)
        }
        _ => LiveTmux::default(),
    }
}
