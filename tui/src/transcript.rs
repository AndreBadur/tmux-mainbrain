//! Click-to-read history (Transcript view) — load a knight's CLEAN turns via
//! the motor's `read_turns --last N` and render them as scrollable content in
//! MAIN, WITHOUT blocking the UI.
//!
//! Why this exists: a live Watch of a full-screen alt-screen knight (kiro-cli,
//! vim, less) has NO vt100 scrollback — the app owns the screen. The honest
//! "scroll inside it" hint is correct for the live pane, but the dev still
//! needs to READ that knight's earlier output. The DURABLE source of a knight's
//! history is its TRANSCRIPT (`read_turns`, clean reconstructed turns), NOT the
//! ephemeral live pane. This worker fetches it off the UI thread and hands back
//! a pre-formatted, scrollable body.
//!
//! Doctrine honored:
//! * GET content via `read_turns` (never `peek`) — the clean, pre-distilled feed.
//! * Non-blocking: the motor call runs on this one-shot worker thread, bounded
//!   by a timeout so the UI never hangs.

use std::sync::mpsc::{RecvTimeoutError, Sender};
use std::time::Duration;

use crate::motor::{MotorClient, Turn};

/// How many recent turns to pull for the transcript view. Both roles.
pub const TRANSCRIPT_LAST: u32 = 50;

/// How long we allow `read_turns` to run before declaring a timeout. It is a
/// local file read on the motor side (fast); a stall is anomalous.
const READ_TIMEOUT: Duration = Duration::from_secs(15);

/// A transcript load request: which knight to read.
#[derive(Debug, Clone)]
pub struct TranscriptRequest {
    pub session_id: String,
    /// Display label (role/agent) for the MAIN title.
    pub title: String,
}

/// Outcome of a transcript load, delivered to the UI over a channel.
#[derive(Debug)]
pub enum TranscriptMsg {
    /// Loaded — `title` echoes the request; `content` is the formatted body.
    Ready { title: String, content: String },
    /// Failed / timed out — show `reason`.
    Failed { title: String, reason: String },
}

/// Spawn a one-shot worker that fetches the transcript and reports on `out`.
/// The thread exits after sending its terminal message.
pub fn spawn(client: MotorClient, req: TranscriptRequest, out: Sender<TranscriptMsg>) {
    std::thread::Builder::new()
        .name(format!("transcript-{}", req.session_id))
        .spawn(move || run(client, req, out))
        .expect("failed to spawn transcript worker");
}

fn run(client: MotorClient, req: TranscriptRequest, out: Sender<TranscriptMsg>) {
    // Run the (blocking) motor call on an inner thread so we can bound it with a
    // timeout without hanging on a stuck subprocess.
    let (tx, rx) = std::sync::mpsc::channel::<Result<Vec<Turn>, String>>();
    {
        let client = client.clone();
        let sid = req.session_id.clone();
        std::thread::Builder::new()
            .name("transcript-read-turns".to_string())
            .spawn(move || {
                let r = client
                    .read_turns_last(&sid, TRANSCRIPT_LAST)
                    .map(|d| d.turns)
                    .map_err(|e| e.to_string());
                let _ = tx.send(r);
            })
            .expect("failed to spawn read_turns thread");
    }

    match rx.recv_timeout(READ_TIMEOUT) {
        Ok(Ok(turns)) => {
            let content = format_transcript(&turns);
            let _ = out.send(TranscriptMsg::Ready {
                title: req.title,
                content,
            });
        }
        Ok(Err(e)) => {
            let _ = out.send(TranscriptMsg::Failed {
                title: req.title,
                reason: format!("read_turns failed: {e}"),
            });
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = out.send(TranscriptMsg::Failed {
                title: req.title,
                reason: format!("read_turns timed out after {}s", READ_TIMEOUT.as_secs()),
            });
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = out.send(TranscriptMsg::Failed {
                title: req.title,
                reason: "transcript worker disconnected unexpectedly".to_string(),
            });
        }
    }
}

/// Render clean turns into a plain-text, line-oriented body for the scrollable
/// MAIN content view. Pure (no I/O) so it is unit-testable. Each turn gets a
/// role header (with optional timestamp) followed by its text, blank-separated.
pub fn format_transcript(turns: &[Turn]) -> String {
    if turns.is_empty() {
        return "(no turns recorded for this knight yet)".to_string();
    }
    let mut out = String::new();
    for (i, t) in turns.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        let ts = t
            .ts
            .as_deref()
            .map(|s| format!("  [{s}]"))
            .unwrap_or_default();
        out.push_str(&format!("── {}{} ──\n", t.role.to_uppercase(), ts));
        out.push_str(t.text.trim_end());
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn turn(role: &str, text: &str, ts: Option<&str>) -> Turn {
        Turn {
            role: role.to_string(),
            text: text.to_string(),
            ts: ts.map(String::from),
        }
    }

    #[test]
    fn empty_turns_produce_placeholder() {
        assert!(format_transcript(&[]).contains("no turns"));
    }

    #[test]
    fn formats_role_headers_and_text() {
        let turns = vec![
            turn("user", "hello there", Some("2026-09-30T00:00:00Z")),
            turn("assistant", "hi back", None),
        ];
        let body = format_transcript(&turns);
        // Role headers upper-cased; user timestamp present; both texts included.
        assert!(body.contains("── USER  [2026-09-30T00:00:00Z] ──"));
        assert!(body.contains("hello there"));
        assert!(body.contains("── ASSISTANT ──"));
        assert!(body.contains("hi back"));
    }

    #[test]
    fn multiline_turn_text_is_preserved() {
        let turns = vec![turn("assistant", "line one\nline two\nline three", None)];
        let body = format_transcript(&turns);
        let n_lines = body.lines().count();
        // header + 3 content lines (trailing blank trimmed by trim_end + push).
        assert!(n_lines >= 4, "multiline text preserved, got {n_lines} lines");
        assert!(body.contains("line one"));
        assert!(body.contains("line three"));
    }
}
