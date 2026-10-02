//! The motor boundary — the ONE module that shells out to the Python motor.
//!
//! The TUI is Rust; the motor is Python (`~/tmux-mainbrain/motor`). We never
//! reimplement session parsing here — we invoke `python3 -m motor <verb>` from
//! the motor working directory, capture stdout, and parse the single JSON
//! document it prints (per the motor CLI contract: one JSON doc to stdout,
//! exit 0 on success; `{"error", "type"}` + non-zero on failure).
//!
//! Keeping the boundary in this module means the rest of the TUI is
//! boundary-agnostic: it only ever sees typed [`Knight`] / [`Turn`] values and
//! a typed [`MotorError`].

use std::path::PathBuf;
use std::process::Command;

use serde::Deserialize;

/// Typed failure for any motor invocation. Distinguishes "we could not run the
/// process" from "the motor reported a structured error" from "we could not
/// parse the output".
#[derive(Debug)]
pub enum MotorError {
    /// The `python3` process could not be spawned or failed at the OS level.
    Spawn(String),
    /// The motor ran but exited non-zero (or printed an `{"error": ...}` doc).
    Motor { verb: String, message: String },
    /// stdout was not the JSON shape we expected.
    Parse { verb: String, message: String },
}

impl std::fmt::Display for MotorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MotorError::Spawn(m) => write!(f, "failed to spawn python3 -m motor: {m}"),
            MotorError::Motor { verb, message } => {
                write!(f, "motor {verb} error: {message}")
            }
            MotorError::Parse { verb, message } => {
                write!(f, "motor {verb} parse error: {message}")
            }
        }
    }
}

impl std::error::Error for MotorError {}

/// The motor's structured error document (`{"error": "...", "type": "..."}`).
#[derive(Debug, Deserialize)]
struct MotorErrorDoc {
    error: String,
    #[serde(default)]
    r#type: Option<String>,
}

/// One knight (session) as reported by `scan_knights` / `context_of`.
///
/// Field shapes verified against the live motor on 2026-09-29:
/// `scan_knights` → `{count, knights: [{agent, alive, context_pct, essence,
/// format, last_seen, parent, purpose, salons, session_id, window}]}`.
///
/// NOTE (data finding): the index exposes `alive: bool` (+ `context_of` adds a
/// `liveness` string), NOT an IDLE/BUSY/GATE process state. Fine-grained
/// BUSY/GATE state is a live-pane concern read via tmux `peek` at watch time
/// (M3), not something the index can supply. [`Knight::state`] maps what the
/// index CAN tell us.
#[derive(Debug, Clone, Deserialize)]
pub struct Knight {
    pub session_id: String,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub alive: bool,
    #[serde(default)]
    pub context_pct: Option<f64>,
    #[serde(default)]
    pub purpose: Option<String>,
    /// The parent session id from the scan_knights index — the cascade edge in
    /// the index. SUPERSEDED as the tree source by `journeys/*/meta.json` (King
    /// ruling, Option A): the index `parent` is sparse (2/281) and does not
    /// carry the governed command hierarchy. Kept to document the index shape.
    #[allow(dead_code)]
    #[serde(default)]
    pub parent: Option<String>,
}

/// What the index can tell us about a knight's liveness. Fine-grained
/// IDLE/BUSY/GATE needs a live `peek` (M3, watch time) — the index only has
/// `alive`/`liveness`. The cascade join in [`crate::tree`] consumes `alive`
/// directly, so we expose the boolean rather than a redundant enum here.
impl Knight {
    /// A short display label for the agent (falls back to `<unknown>`).
    pub fn agent_label(&self) -> &str {
        self.agent.as_deref().unwrap_or("<unknown>")
    }
}

/// The `scan_knights` top-level document.
#[derive(Debug, Deserialize)]
struct ScanKnightsDoc {
    #[serde(default)]
    knights: Vec<Knight>,
}

/// One clean turn from `read_turns`.
///
/// Part of the reactive feed (`read_turns`), consumed by the TRANSCRIPT view.
#[derive(Debug, Clone, Deserialize)]
pub struct Turn {
    pub role: String,
    pub text: String,
    #[serde(default)]
    pub ts: Option<String>,
}

/// The `read_turns` document: the delta plus a new byte-offset cursor.
///
/// Consumed by the TRANSCRIPT view (`read_turns --last N`) and the incremental
/// reactive feed (`read_turns --since <cursor>`).
#[derive(Debug, Clone, Deserialize)]
pub struct TurnsDelta {
    #[serde(default)]
    pub turns: Vec<Turn>,
    /// New byte-offset cursor to pass as `--since` on the next incremental read.
    /// Read by [`crate::motor::MotorClient::read_turns_since`] (the incremental
    /// feed); the `--last N` transcript path ignores it. Kept to document the
    /// wire shape and back future incremental reads.
    #[allow(dead_code)]
    #[serde(default)]
    pub cursor: u64,
}

/// A thin, cloneable client bound to the motor's working directory. Cloning is
/// cheap (just the cwd path) so worker threads can each hold one.
#[derive(Debug, Clone)]
pub struct MotorClient {
    /// Working directory to invoke the motor from (`~/tmux-mainbrain`).
    cwd: PathBuf,
}

impl MotorClient {
    /// Build a client that runs `python3 -m motor` from `cwd`.
    pub fn new(cwd: impl Into<PathBuf>) -> Self {
        Self { cwd: cwd.into() }
    }

    /// Run a verb, returning parsed stdout as raw JSON `Value` on success.
    fn run(&self, verb: &str, args: &[&str]) -> Result<serde_json::Value, MotorError> {
        let output = Command::new("python3")
            .arg("-m")
            .arg("motor")
            .arg(verb)
            .args(args)
            .current_dir(&self.cwd)
            .output()
            .map_err(|e| MotorError::Spawn(e.to_string()))?;

        let stdout = String::from_utf8_lossy(&output.stdout);

        // The motor prints an {"error","type"} doc on failure. Try to surface
        // that before falling back to a generic status message.
        if !output.status.success() {
            if let Ok(doc) = serde_json::from_str::<MotorErrorDoc>(stdout.trim()) {
                let kind = doc.r#type.unwrap_or_else(|| "error".to_string());
                return Err(MotorError::Motor {
                    verb: verb.to_string(),
                    message: format!("[{kind}] {}", doc.error),
                });
            }
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(MotorError::Motor {
                verb: verb.to_string(),
                message: format!(
                    "exit {}: {}",
                    output.status.code().unwrap_or(-1),
                    stderr.trim()
                ),
            });
        }

        serde_json::from_str(stdout.trim()).map_err(|e| MotorError::Parse {
            verb: verb.to_string(),
            message: e.to_string(),
        })
    }

    /// `scan_knights` — the full knight index (cascade node list). Sorted by
    /// agent then session id for a stable UI ordering.
    pub fn scan_knights(&self) -> Result<Vec<Knight>, MotorError> {
        let value = self.run("scan_knights", &[])?;
        let doc: ScanKnightsDoc =
            serde_json::from_value(value).map_err(|e| MotorError::Parse {
                verb: "scan_knights".to_string(),
                message: e.to_string(),
            })?;
        let mut knights = doc.knights;
        // Order for the UI: alive knights first (what the dev wants to watch),
        // then named before unknown, then by agent, then session id — stable.
        knights.sort_by(|a, b| {
            b.alive
                .cmp(&a.alive)
                .then_with(|| a.agent.is_none().cmp(&b.agent.is_none()))
                .then_with(|| a.agent_label().cmp(b.agent_label()))
                .then_with(|| a.session_id.cmp(&b.session_id))
        });
        Ok(knights)
    }

    /// `read_turns <sid> --since <cursor>` — incremental clean-turn tail. Pass
    /// the `cursor` from the previous [`TurnsDelta`] to read only the delta.
    ///
    /// The reactive feed; wired into the cascade live-tail in M2.
    #[allow(dead_code)]
    pub fn read_turns_since(
        &self,
        session_id: &str,
        cursor: u64,
    ) -> Result<TurnsDelta, MotorError> {
        let since = cursor.to_string();
        let value = self.run("read_turns", &[session_id, "--since", &since])?;
        serde_json::from_value(value).map_err(|e| MotorError::Parse {
            verb: "read_turns".to_string(),
            message: e.to_string(),
        })
    }

    /// `read_turns <sid> --last <n>` — the most recent `n` clean turns (both
    /// roles). Used by the TRANSCRIPT view: the DURABLE history source for any
    /// knight, including full-screen alt-screen ones whose live pane keeps no
    /// scrollback. Blocking → call on a worker thread.
    pub fn read_turns_last(&self, session_id: &str, last: u32) -> Result<TurnsDelta, MotorError> {
        let last_s = last.to_string();
        let value = self.run("read_turns", &[session_id, "--last", &last_s])?;
        serde_json::from_value(value).map_err(|e| MotorError::Parse {
            verb: "read_turns".to_string(),
            message: e.to_string(),
        })
    }

    /// `resume_clean <session_id> --tmux <name> [--parent .. --journey .. --role ..]`
    /// — resume with automatic DEAD-owner-lock clearing. The motor:
    /// * lock absent OR dead-pid → removes the dead lock (if any) and resumes →
    ///   `{resumed:true, tmux_session, removed_stale_lock:bool, ...}`.
    /// * lock held by a LIVE pid → REFUSES (never kills/deletes a live lock) →
    ///   `{resumed:false, reason:"held_by_live", pid, ...}`.
    /// * unparseable lock / missing session → `{resumed:false, reason:..}`.
    ///
    /// Optional lineage args are forwarded so a meta.json record stays correct.
    /// Blocking → call on a worker thread.
    pub fn resume_clean(
        &self,
        session_id: &str,
        tmux_session: &str,
        parent: Option<&str>,
        journey: Option<&str>,
        role: Option<&str>,
    ) -> Result<ResumeCleanResult, MotorError> {
        let mut args: Vec<&str> = vec![session_id, "--tmux", tmux_session];
        if let Some(p) = parent {
            args.push("--parent");
            args.push(p);
        }
        if let Some(j) = journey {
            args.push("--journey");
            args.push(j);
        }
        if let Some(r) = role {
            args.push("--role");
            args.push(r);
        }
        let value = self.run("resume_clean", &args)?;
        serde_json::from_value(value).map_err(|e| MotorError::Parse {
            verb: "resume_clean".to_string(),
            message: e.to_string(),
        })
    }
}

/// The `resume_clean` result document. `resumed` is the outcome flag; on refuse
/// the `reason` (+ `pid` for the live case) explains why.
#[derive(Debug, Clone, Deserialize)]
pub struct ResumeCleanResult {
    #[serde(default)]
    pub resumed: bool,
    /// The tmux the motor attached to on success (may differ from requested).
    #[serde(default)]
    pub tmux_session: Option<String>,
    /// True when a dead-owner stale lock was removed to allow the resume.
    #[serde(default)]
    pub removed_stale_lock: bool,
    /// Refusal reason on `resumed:false`: "held_by_live" | "unknown_lock" |
    /// "session_not_found" | other.
    #[serde(default)]
    pub reason: Option<String>,
    /// The live owner PID when `reason == "held_by_live"`.
    #[serde(default)]
    pub pid: Option<u32>,
    /// Optional human detail (e.g. for session_not_found).
    #[serde(default)]
    pub detail: Option<String>,
}
