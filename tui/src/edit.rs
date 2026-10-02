//! The interactive EDIT view — an embedded pty running `$EDITOR <file>` inside
//! the MAIN block, built on the shared [`PtySession`] component (King Option 1:
//! reuse the pty widget, do NOT build a native editor).
//!
//! `EditSession` = a `PtySession` running the resolved editor on a file, cwd =
//! the file's directory. On normal flow the editor process quits itself; the UI
//! detects the exit and returns MAIN to a fresh read-only preview of the file.
//! No special teardown: `PtySession::drop` force-kills (if still running, e.g.
//! Ctrl+x c cancel) and reaps the child — no zombies.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use portable_pty::CommandBuilder;

use crate::pty::PtySession;

/// Candidate editors tried (in order) when neither `$EDITOR` nor `$VISUAL` is
/// set. All commonly present on Linux hosts.
const FALLBACK_EDITORS: &[&str] = &["nano", "vi", "vim"];

/// A running edit session bound to one file path.
pub struct EditSession {
    /// The file being edited (absolute path).
    pub file: PathBuf,
    /// The editor program actually launched (for the title / status).
    pub editor: String,
    pty: PtySession,
}

impl EditSession {
    /// Open the resolved editor on `file`, sized to `rows`x`cols`. cwd is set to
    /// the file's parent directory. Errors if no editor can be found.
    pub fn open(file: &Path, rows: u16, cols: u16) -> Result<Self> {
        let editor = resolve_editor()
            .ok_or_else(|| anyhow!("no editor found; set $EDITOR (tried $EDITOR, $VISUAL, nano/vi/vim)"))?;

        // Split the resolved editor string so `$EDITOR="code -w"`-style values
        // work: first token = program, rest = leading args, then the file.
        let mut parts = editor.split_whitespace();
        let prog = parts
            .next()
            .ok_or_else(|| anyhow!("empty editor command"))?;
        let mut cmd = CommandBuilder::new(prog);
        for arg in parts {
            cmd.arg(arg);
        }
        cmd.arg(file.as_os_str());
        if let Some(dir) = file.parent() {
            if !dir.as_os_str().is_empty() {
                cmd.cwd(dir);
            }
        }
        cmd.env("TERM", "xterm-256color");

        let label = file
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "edit".to_string());
        let pty = PtySession::spawn(cmd, rows, cols, false, &label)
            .with_context(|| format!("failed to launch editor `{prog}`"))?;

        Ok(Self {
            file: file.to_path_buf(),
            editor: prog.to_string(),
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

/// Resolve the editor to use: `$EDITOR` (if set and non-empty) → `$VISUAL` →
/// the first of `nano`, `vi`, `vim` found on `$PATH`. Returns the command
/// string (may include args, e.g. "vim -p"), or None if nothing is available.
pub fn resolve_editor() -> Option<String> {
    resolve_editor_with(|k| std::env::var(k).ok(), program_on_path)
}

/// Testable core of [`resolve_editor`]: `get_env` looks up an env var, `exists`
/// reports whether a bare program name is on PATH.
fn resolve_editor_with(
    get_env: impl Fn(&str) -> Option<String>,
    exists: impl Fn(&str) -> bool,
) -> Option<String> {
    for var in ["EDITOR", "VISUAL"] {
        if let Some(v) = get_env(var) {
            let v = v.trim();
            if !v.is_empty() {
                return Some(v.to_string());
            }
        }
    }
    for cand in FALLBACK_EDITORS {
        if exists(cand) {
            return Some((*cand).to_string());
        }
    }
    None
}

/// Is `prog` an executable on `$PATH`? Pure filesystem probe (no fork).
fn program_on_path(prog: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| {
        let full = dir.join(prog);
        full.is_file() && is_executable(&full)
    })
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(_path: &Path) -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_map(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
    }

    #[test]
    fn editor_var_wins_when_set() {
        let env = env_map(&[("EDITOR", "myed"), ("VISUAL", "other")]);
        let got = resolve_editor_with(|k| env.get(k).cloned(), |_| true);
        assert_eq!(got.as_deref(), Some("myed"));
    }

    #[test]
    fn visual_used_when_editor_unset_or_empty() {
        // EDITOR empty → skip to VISUAL.
        let env = env_map(&[("EDITOR", "   "), ("VISUAL", "ved")]);
        let got = resolve_editor_with(|k| env.get(k).cloned(), |_| true);
        assert_eq!(got.as_deref(), Some("ved"));
    }

    #[test]
    fn editor_unset_falls_back_to_nano_then_vi_then_vim() {
        // $EDITOR AND $VISUAL unset (the real host condition). Only vi exists.
        let none_env = |_: &str| None;
        let only_vi = |p: &str| p == "vi";
        assert_eq!(
            resolve_editor_with(none_env, only_vi).as_deref(),
            Some("vi"),
            "falls through nano (absent) to vi"
        );
        // nano present → nano wins (first in the fallback order).
        let nano_and_vim = |p: &str| p == "nano" || p == "vim";
        assert_eq!(
            resolve_editor_with(none_env, nano_and_vim).as_deref(),
            Some("nano")
        );
        // only vim.
        let only_vim = |p: &str| p == "vim";
        assert_eq!(
            resolve_editor_with(none_env, only_vim).as_deref(),
            Some("vim")
        );
    }

    #[test]
    fn no_editor_anywhere_returns_none() {
        let got = resolve_editor_with(|_| None, |_| false);
        assert_eq!(got, None, "no $EDITOR/$VISUAL and no fallback → None");
    }
}
