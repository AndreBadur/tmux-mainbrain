//! Click-to-revive (Feature A) — wake an OFFLINE knight's existing session and
//! attach it in MAIN, without blocking the UI.
//!
//! Doctrine honored:
//! * RESUME the existing `session_id` (context intact) — never a fresh spawn.
//! * Runtime-pointer only: the caller updates `tmux_session` in meta.json; this
//!   module never touches parent/level/role/tree.
//! * DEAD-owner stale locks are auto-cleared by the motor's `resume_clean` verb
//!   (no more manual-`rm` guidance). A LIVE-owner lock is REFUSED by the motor
//!   and surfaced verbatim — we never kill a process or delete a live lock.
//! * Non-blocking: `resume_clean` runs on this worker thread, bounded by a
//!   timeout so the UI never hangs.

use std::sync::mpsc::{RecvTimeoutError, Sender};
use std::time::Duration;

use crate::motor::MotorClient;

/// A revive request: which offline knight to wake.
#[derive(Debug, Clone)]
pub struct ReviveRequest {
    pub session_id: String,
    pub tmux_session: String,
    /// Display label for status messages.
    pub label: String,
}

/// Outcome of a revive attempt, delivered to the UI over a channel.
#[derive(Debug)]
pub enum ReviveMsg {
    /// Progress text to show in MAIN while resuming.
    Progress {
        /// Knight label (carried for symmetry with the other variants; MAIN
        /// already shows the label from its `Reviving` state).
        #[allow(dead_code)]
        label: String,
        text: String,
    },
    /// Resume succeeded — attach this tmux session in MAIN now. `cleared_lock`
    /// is true when a dead-owner stale lock was auto-removed to allow it.
    Ready {
        label: String,
        session_id: String,
        tmux_session: String,
        cleared_lock: bool,
    },
    /// Resume refused / failed / timed out. Show `reason` in MAIN. No attach.
    Failed { label: String, reason: String },
}

/// How long we allow `resume_clean` to run before declaring a timeout. Resume
/// itself is usually fast (it spawns the tmux runtime); a long stall almost
/// always means the kiro-cli inside is stuck initializing.
const RESUME_TIMEOUT: Duration = Duration::from_secs(20);

/// Spawn a one-shot worker thread that performs the revive and reports back on
/// `out`. The thread exits after sending its terminal message.
pub fn spawn(client: MotorClient, req: ReviveRequest, out: Sender<ReviveMsg>) {
    std::thread::Builder::new()
        .name(format!("revive-{}", req.tmux_session))
        .spawn(move || run(client, req, out))
        .expect("failed to spawn revive worker");
}

fn run(client: MotorClient, req: ReviveRequest, out: Sender<ReviveMsg>) {
    let _ = out.send(ReviveMsg::Progress {
        label: req.label.clone(),
        text: format!("resuming {} (auto-clearing any stale lock)…", req.tmux_session),
    });

    // Run `resume_clean` on an inner thread so we can bound it with a timeout
    // (the UI thread is already free — this bounds a stuck resume). The verb
    // itself auto-clears a DEAD-owner lock and refuses a LIVE-owner one.
    let (tx, rx) = std::sync::mpsc::channel::<Result<CleanOutcome, String>>();
    {
        let client = client.clone();
        let sid = req.session_id.clone();
        let tmux = req.tmux_session.clone();
        std::thread::Builder::new()
            .name("revive-resume-clean".to_string())
            .spawn(move || {
                // We deliberately do NOT pass --journey/--parent here: the
                // knight ALREADY exists in meta.json with correct lineage, and
                // the motor's record-on-resume would coarsely rewrite the entry
                // (clobbering agent/note). The TUI keeps its own pointer-ONLY
                // meta update (meta_writer) as the authority. resume_clean is
                // used purely for its dead-lock-auto-clear + live-lock-refuse.
                let r = client
                    .resume_clean(&sid, &tmux, None, None, None)
                    .map(CleanOutcome::from_result)
                    .map_err(|e| e.to_string());
                let _ = tx.send(r);
            })
            .expect("failed to spawn resume_clean thread");
    }

    match rx.recv_timeout(RESUME_TIMEOUT) {
        Ok(Ok(CleanOutcome::Resumed {
            tmux_session,
            cleared_lock,
        })) => {
            let _ = out.send(ReviveMsg::Ready {
                label: req.label,
                session_id: req.session_id,
                // Prefer the motor-resolved tmux; fall back to the requested one.
                tmux_session: tmux_session.unwrap_or(req.tmux_session),
                cleared_lock,
            });
        }
        Ok(Ok(CleanOutcome::HeldByLive { pid })) => {
            // Safety case: never resume over a live owner. Surface it clearly.
            let pid_txt = pid
                .map(|p| format!("pid {p}"))
                .unwrap_or_else(|| "unknown pid".to_string());
            let _ = out.send(ReviveMsg::Failed {
                label: req.label,
                reason: format!(
                    "session is locked by a LIVE process ({pid_txt}) — it may already be \
                     running elsewhere; not resuming to avoid a conflict."
                ),
            });
        }
        Ok(Ok(CleanOutcome::Refused { reason, detail })) => {
            let detail = detail
                .map(|d| format!(" — {d}"))
                .unwrap_or_default();
            let _ = out.send(ReviveMsg::Failed {
                label: req.label,
                reason: format!("cannot resume {}: {reason}{detail}", req.tmux_session),
            });
        }
        Ok(Err(e)) => {
            let _ = out.send(ReviveMsg::Failed {
                label: req.label,
                reason: format!("resume_clean failed: {e}"),
            });
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = out.send(ReviveMsg::Failed {
                label: req.label,
                reason: format!(
                    "resume timed out after {}s (likely stuck initializing); try \
                     `python3 -m motor resume_clean {} --tmux {}` manually.",
                    RESUME_TIMEOUT.as_secs(),
                    req.session_id,
                    req.tmux_session
                ),
            });
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = out.send(ReviveMsg::Failed {
                label: req.label,
                reason: "resume worker disconnected unexpectedly".to_string(),
            });
        }
    }
}

/// A normalized `resume_clean` outcome (decouples the worker's match from the
/// raw motor struct + makes it unit-testable).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CleanOutcome {
    /// Resumed successfully; `cleared_lock` = a dead lock was auto-removed.
    Resumed {
        tmux_session: Option<String>,
        cleared_lock: bool,
    },
    /// Refused because a LIVE process holds the lock (the safety case).
    HeldByLive { pid: Option<u32> },
    /// Refused for another reason (unknown_lock / session_not_found / …).
    Refused {
        reason: String,
        detail: Option<String>,
    },
}

impl CleanOutcome {
    /// Classify a raw `resume_clean` result into a [`CleanOutcome`].
    pub fn from_result(r: crate::motor::ResumeCleanResult) -> Self {
        if r.resumed {
            return CleanOutcome::Resumed {
                tmux_session: r.tmux_session,
                cleared_lock: r.removed_stale_lock,
            };
        }
        match r.reason.as_deref() {
            Some("held_by_live") => CleanOutcome::HeldByLive { pid: r.pid },
            other => CleanOutcome::Refused {
                reason: other.unwrap_or("unknown").to_string(),
                detail: r.detail,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::motor::ResumeCleanResult;

    fn res(
        resumed: bool,
        tmux: Option<&str>,
        removed: bool,
        reason: Option<&str>,
        pid: Option<u32>,
        detail: Option<&str>,
    ) -> ResumeCleanResult {
        ResumeCleanResult {
            resumed,
            tmux_session: tmux.map(String::from),
            removed_stale_lock: removed,
            reason: reason.map(String::from),
            pid,
            detail: detail.map(String::from),
        }
    }

    #[test]
    fn resumed_true_maps_to_resumed_with_cleared_flag() {
        let out = CleanOutcome::from_result(res(
            true,
            Some("arch-tui"),
            true,
            None,
            None,
            None,
        ));
        assert_eq!(
            out,
            CleanOutcome::Resumed {
                tmux_session: Some("arch-tui".to_string()),
                cleared_lock: true,
            }
        );
    }

    #[test]
    fn resumed_without_clearing_reports_cleared_false() {
        let out = CleanOutcome::from_result(res(true, Some("t"), false, None, None, None));
        assert_eq!(
            out,
            CleanOutcome::Resumed {
                tmux_session: Some("t".to_string()),
                cleared_lock: false,
            }
        );
    }

    #[test]
    fn held_by_live_maps_to_refuse_with_pid() {
        let out = CleanOutcome::from_result(res(
            false,
            None,
            false,
            Some("held_by_live"),
            Some(4242),
            None,
        ));
        assert_eq!(out, CleanOutcome::HeldByLive { pid: Some(4242) });
    }

    #[test]
    fn session_not_found_maps_to_refused_reason() {
        let out = CleanOutcome::from_result(res(
            false,
            None,
            false,
            Some("session_not_found"),
            None,
            Some("no session dir"),
        ));
        assert_eq!(
            out,
            CleanOutcome::Refused {
                reason: "session_not_found".to_string(),
                detail: Some("no session dir".to_string()),
            }
        );
    }

    #[test]
    fn unknown_lock_maps_to_refused() {
        let out =
            CleanOutcome::from_result(res(false, None, false, Some("unknown_lock"), None, None));
        assert_eq!(
            out,
            CleanOutcome::Refused {
                reason: "unknown_lock".to_string(),
                detail: None,
            }
        );
    }
}
