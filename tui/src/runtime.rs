//! Runtime presence — the AUTHORITY for "does a tmux runtime exist to attach
//! to?", which is a DIFFERENT question from the motor's liveness ("is the
//! conversation fresh?").
//!
//! Bug fix (King-ruled): an IDLE knight whose tmux is alive but whose session
//! file is >15min old reports `alive:false / liveness:"stale"` from the motor.
//! That must NOT be read as "offline" — its runtime is present and attachable.
//! tmux existence is the source of truth for runtime presence; motor liveness
//! only feeds the context% and the "fresh conversation" nuance.

use std::collections::HashSet;

/// The set of tmux session NAMES currently alive (one `tmux list-sessions` per
/// poll cycle — never N forks). Wrapped so callers can't confuse it with other
/// string sets.
#[derive(Debug, Clone, Default)]
pub struct LiveTmux {
    names: HashSet<String>,
}

impl LiveTmux {
    pub fn from_names(names: impl IntoIterator<Item = String>) -> Self {
        Self {
            names: names.into_iter().collect(),
        }
    }

    /// Does a tmux session with this exact name exist?
    pub fn contains(&self, name: &str) -> bool {
        self.names.contains(name)
    }
}

/// Resolve the tmux session name for a knight: the recorded `tmux_session` if
/// present, else a stable derived name from label + session id. The SAME
/// function is used by the tree (to set the glyph) and by the watch/revive
/// decision (to check existence) so they never disagree.
pub fn resolve_tmux_name(
    recorded: Option<&str>,
    label: &str,
    session_id: &str,
) -> String {
    match recorded {
        Some(name) if !name.is_empty() => name.to_string(),
        _ => derive_tmux_name(label, session_id),
    }
}

/// Derive a stable tmux session name for a knight with no recorded one:
/// `<label-sanitized>-<sid8>`. Idempotent, so repeated revives land on the same
/// name.
pub fn derive_tmux_name(label: &str, session_id: &str) -> String {
    let clean: String = label
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
        .collect();
    let clean = clean.trim_matches('-');
    let short = session_id
        .strip_prefix("sess_")
        .unwrap_or(session_id)
        .chars()
        .take(8)
        .collect::<String>();
    if clean.is_empty() {
        format!("revive-{short}")
    } else {
        format!("{clean}-{short}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recorded_name_wins_over_derived() {
        assert_eq!(
            resolve_tmux_name(Some("archmaester-tui"), "archmaester", "sess_4e1d2bb4"),
            "archmaester-tui"
        );
    }

    #[test]
    fn derives_stable_name_when_unrecorded() {
        let a = resolve_tmux_name(None, "victim", "sess_341bcfbc-9ad2");
        let b = resolve_tmux_name(Some(""), "victim", "sess_341bcfbc-9ad2");
        assert_eq!(a, "victim-341bcfbc");
        assert_eq!(a, b, "empty recorded name falls back to derived");
    }

    #[test]
    fn live_tmux_membership() {
        let live = LiveTmux::from_names(["archmaester-tui".to_string(), "steward-tui".to_string()]);
        assert!(live.contains("archmaester-tui"));
        assert!(!live.contains("coder-tui"));
    }
}
