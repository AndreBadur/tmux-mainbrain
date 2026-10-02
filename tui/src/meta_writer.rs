//! Minimal, safe, pointer-ONLY update of a journey's `meta.json`.
//!
//! Feature A revive doctrine — the TUI is a TOOL, not a commander. On revive we
//! update ONLY the runtime pointer (`tmux_session`) of the target knight. We
//! NEVER touch `parent`, `level`, `role`, `tree_root`, or add/remove knights.
//!
//! Implementation: parse the whole file as an untyped `serde_json::Value`
//! (preserving every other field verbatim), mutate exactly one string, and
//! write atomically (temp file in the same dir + rename). Object key order is
//! preserved via serde_json's `preserve_order` behavior on `Value` when the
//! feature is on; if not, keys may reorder but no data is lost — we accept a
//! possible key reorder as a documented, harmless cosmetic effect.

use std::path::{Path, PathBuf};

/// Find the `journeys/<id>/meta.json` under `root_dir` that lists a knight with
/// `session_id`, and set that knight's `tmux_session` to `tmux`. Returns the
/// path updated on success. Does nothing structural beyond that one field.
pub fn update_tmux_pointer(
    root_dir: &Path,
    session_id: &str,
    tmux: &str,
) -> Result<PathBuf, String> {
    let journeys_dir = root_dir.join("journeys");
    let entries = std::fs::read_dir(&journeys_dir)
        .map_err(|e| format!("read journeys/: {e}"))?;

    for entry in entries.flatten() {
        let path = entry.path().join("meta.json");
        if !path.is_file() {
            continue;
        }
        match try_update_file(&path, session_id, tmux) {
            Ok(true) => return Ok(path),
            Ok(false) => continue,
            Err(e) => return Err(e),
        }
    }
    Err(format!(
        "no meta.json under {} lists session {session_id}",
        journeys_dir.display()
    ))
}

/// Update one file if it contains the target knight. Returns Ok(true) if the
/// pointer was written, Ok(false) if this file does not list the session.
fn try_update_file(path: &Path, session_id: &str, tmux: &str) -> Result<bool, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut doc: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;

    let Some(knights) = doc.get_mut("knights").and_then(|k| k.as_array_mut()) else {
        return Ok(false);
    };

    let mut updated = false;
    for knight in knights.iter_mut() {
        let matches = knight
            .get("session_id")
            .and_then(|s| s.as_str())
            .map(|s| s == session_id)
            .unwrap_or(false);
        if matches {
            // Pointer-only mutation. We deliberately do NOT read or write
            // parent/level/role/tree — only this one runtime field.
            if let Some(obj) = knight.as_object_mut() {
                obj.insert(
                    "tmux_session".to_string(),
                    serde_json::Value::String(tmux.to_string()),
                );
                updated = true;
            }
        }
    }

    if !updated {
        return Ok(false);
    }

    // Serialize pretty (2-space) to match the human-edited style, atomic write.
    let serialized =
        serde_json::to_string_pretty(&doc).map_err(|e| format!("serialize: {e}"))?;
    atomic_write(path, &serialized)?;
    Ok(true)
}

/// Write `contents` to `path` atomically: write a sibling temp file, fsync, then
/// rename over the target (rename is atomic on the same filesystem).
fn atomic_write(path: &Path, contents: &str) -> Result<(), String> {
    use std::io::Write;
    let dir = path.parent().ok_or("meta.json has no parent dir")?;
    let tmp = dir.join(format!(
        ".meta.json.tmp.{}",
        std::process::id()
    ));
    {
        let mut f = std::fs::File::create(&tmp)
            .map_err(|e| format!("create temp {}: {e}", tmp.display()))?;
        f.write_all(contents.as_bytes())
            .map_err(|e| format!("write temp: {e}"))?;
        f.write_all(b"\n").map_err(|e| format!("write temp: {e}"))?;
        f.sync_all().map_err(|e| format!("fsync temp: {e}"))?;
    }
    std::fs::rename(&tmp, path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("rename temp → {}: {e}", path.display())
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_meta(dir: &Path, jid: &str, json: &str) {
        let jdir = dir.join("journeys").join(jid);
        std::fs::create_dir_all(&jdir).unwrap();
        std::fs::write(jdir.join("meta.json"), json).unwrap();
    }

    fn tempdir() -> PathBuf {
        let base = std::env::temp_dir().join(format!(
            "meta-writer-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        base
    }

    #[test]
    fn pointer_update_preserves_parent_level_role() {
        let dir = tempdir();
        let json = r#"{
  "journey_id": "j",
  "tui": "ENABLED",
  "parenting_rule": "parent = who COMMANDS",
  "tree_root": {"role": "king", "level": 1, "session_id": "sess_king"},
  "knights": [
    {
      "role": "coder",
      "agent": "ephemeral-coder-tui",
      "session_id": "sess_coder",
      "tmux_session": "coder-OLD",
      "parent": "sess_king",
      "level": 2,
      "note": "keep me"
    }
  ]
}"#;
        write_meta(&dir, "j", json);

        let updated = update_tmux_pointer(&dir, "sess_coder", "coder-NEW").unwrap();
        let after: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&updated).unwrap()).unwrap();

        let k = &after["knights"][0];
        // The ONLY change: tmux_session.
        assert_eq!(k["tmux_session"], "coder-NEW");
        // Everything else preserved verbatim.
        assert_eq!(k["parent"], "sess_king");
        assert_eq!(k["level"], 2);
        assert_eq!(k["role"], "coder");
        assert_eq!(k["note"], "keep me");
        // Top-level fields preserved.
        assert_eq!(after["tui"], "ENABLED");
        assert_eq!(after["parenting_rule"], "parent = who COMMANDS");
        assert_eq!(after["tree_root"]["session_id"], "sess_king");
        assert_eq!(after["tree_root"]["level"], 1);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn missing_session_reports_error_and_changes_nothing() {
        let dir = tempdir();
        let json = r#"{"journey_id":"j","knights":[{"session_id":"sess_a","tmux_session":"a"}]}"#;
        write_meta(&dir, "j", json);
        let before = std::fs::read_to_string(dir.join("journeys/j/meta.json")).unwrap();

        let res = update_tmux_pointer(&dir, "sess_NOPE", "x");
        assert!(res.is_err(), "unknown session → error");

        let after = std::fs::read_to_string(dir.join("journeys/j/meta.json")).unwrap();
        assert_eq!(before, after, "file untouched when session not found");

        std::fs::remove_dir_all(&dir).ok();
    }
}
