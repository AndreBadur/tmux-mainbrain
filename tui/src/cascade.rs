//! The governed cascade — read from `journeys/*/meta.json` (King ruling,
//! Option A: meta.json is the AUTHORITATIVE command tree, not scan_knights'
//! sparse parent-ids).
//!
//! Two meta.json shapes exist in the wild (verified 2026-09-29):
//!
//! * **New format** (`journey-tui`): a top-level `tree_root` (the King) plus
//!   per-knight `parent` (the session that COMMANDS it) and `level`. We nest
//!   each knight under the entry whose `session_id == parent`, giving the true
//!   L1→L2→L3 hierarchy.
//! * **Old format** (the other journeys): no `tree_root`/`parent`/`level`, only
//!   a `king_session`. We root the tree at `king_session` and hang every knight
//!   directly under it (implicit L2) — we do NOT invent deeper edges.
//!
//! PARENTING RULE (respected as-is, never "corrected"): a knight's parent is
//! who COMMANDS it, never who forged it. We trust the `parent` field verbatim.
//!
//! Live state (context% / alive) is joined on `session_id` from the
//! scan_knights index by the caller — this module is pure meta.json parsing.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// One knight entry as stored in a journey's meta.json. All the runtime/enrich
/// fields are optional because the two formats differ.
#[derive(Debug, Clone, Deserialize)]
pub struct MetaKnight {
    pub role: Option<String>,
    pub agent: Option<String>,
    pub session_id: String,
    pub tmux_session: Option<String>,
    /// The COMMANDING session id (new format). Absent in old format.
    pub parent: Option<String>,
    /// 1=King, 2=task-knight, 3=specialist (new format). Absent in old format.
    pub level: Option<u8>,
}

/// The top-level `tree_root` marker (new format only).
#[derive(Debug, Clone, Deserialize)]
pub struct TreeRoot {
    pub role: Option<String>,
    pub session_id: String,
}

/// A journey's meta.json document (superset of both formats).
#[derive(Debug, Clone, Deserialize)]
pub struct JourneyMeta {
    pub journey_id: String,
    pub king_session: Option<String>,
    pub new_king_session: Option<String>,
    pub tree_root: Option<TreeRoot>,
    /// Per-journey TUI visibility tag: `"ENABLED"` shows the journey in the
    /// cascade, `"DISABLED"` (any non-ENABLED value) hides its whole subtree.
    /// MISSING = ENABLED (backward-compatible for old-format journeys).
    #[serde(default)]
    pub tui: Option<String>,
    #[serde(default)]
    pub knights: Vec<MetaKnight>,
}

impl JourneyMeta {
    /// Whether this journey should appear in the cascade. Missing tag →
    /// enabled; only an explicit non-`ENABLED` value hides it.
    pub fn is_tui_enabled(&self) -> bool {
        match &self.tui {
            None => true,
            Some(v) => v.eq_ignore_ascii_case("ENABLED"),
        }
    }
}

impl JourneyMeta {
    /// The authoritative root session id for this journey's tree:
    /// `tree_root` (new) → `new_king_session` → `king_session` (old).
    pub fn root_session(&self) -> Option<String> {
        self.tree_root
            .as_ref()
            .map(|r| r.session_id.clone())
            .or_else(|| self.new_king_session.clone())
            .or_else(|| self.king_session.clone())
    }
}

/// A node in the governed cascade: a session with its meta identity and its
/// children. Live state is attached later (see `tree` module).
#[derive(Debug, Clone)]
pub struct CascadeNode {
    pub session_id: String,
    /// Role label ("king"/"steward"/"coder"/…) when known.
    pub role: Option<String>,
    pub agent: Option<String>,
    pub tmux_session: Option<String>,
    /// Declared level from meta.json, if any.
    pub level: Option<u8>,
    pub children: Vec<CascadeNode>,
}

/// One journey's governed tree.
#[derive(Debug, Clone)]
pub struct JourneyTree {
    pub journey_id: String,
    pub root: CascadeNode,
}

/// The whole governed field: one tree per journey, plus the set of every
/// session id that appears anywhere in a meta.json (so the caller can compute
/// the "unlinked" scan_knights remainder).
#[derive(Debug, Clone, Default)]
pub struct Cascade {
    pub journeys: Vec<JourneyTree>,
    /// Every governed session id (roots + knights) across all journeys.
    pub governed: std::collections::HashSet<String>,
}

/// Read and parse a single meta.json into a [`JourneyTree`]. Returns `Ok(None)`
/// if the file has no resolvable root (nothing to anchor a tree on).
fn parse_journey(path: &Path) -> Result<Option<JourneyTree>, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    let meta: JourneyMeta = serde_json::from_str(&text)
        .map_err(|e| format!("{}: {e}", path.display()))?;

    // Feature B: per-journey TUI filter. A DISABLED journey is hidden entirely
    // (whole subtree omitted). Missing tag = ENABLED.
    if !meta.is_tui_enabled() {
        return Ok(None);
    }

    let Some(root_sid) = meta.root_session() else {
        return Ok(None);
    };

    // Index knights by session id and by parent for nesting.
    let mut by_parent: BTreeMap<String, Vec<MetaKnight>> = BTreeMap::new();
    let mut orphans: Vec<MetaKnight> = Vec::new();

    let has_parent_links = meta.knights.iter().any(|k| k.parent.is_some());

    for k in &meta.knights {
        // Skip an entry that IS the root (some metas may list the king).
        if k.session_id == root_sid {
            continue;
        }
        match &k.parent {
            Some(p) => by_parent.entry(p.clone()).or_default().push(k.clone()),
            None => orphans.push(k.clone()),
        }
    }

    // Build the root node.
    let root_role = meta
        .tree_root
        .as_ref()
        .and_then(|r| r.role.clone())
        .or(Some("king".to_string()));

    let mut root = CascadeNode {
        session_id: root_sid.clone(),
        role: root_role,
        agent: None,
        tmux_session: None,
        level: Some(1),
        children: Vec::new(),
    };

    if has_parent_links {
        // New format: nest recursively by parent id.
        root.children = build_children(&root_sid, &mut by_parent);
        // Any knight whose parent was not found (dangling) → hang under root so
        // it is never dropped (the tree is authoritative even if imperfect).
        for k in by_parent.values().flatten() {
            root.children.push(leaf(k));
        }
    } else {
        // Old format: no parent links → every knight is an implicit L2 child of
        // the King. Do not invent deeper structure.
        for k in &meta.knights {
            if k.session_id != root_sid {
                root.children.push(leaf(k));
            }
        }
    }

    // Orphans in a new-format meta (parent field absent on some entries) also
    // hang under the root rather than being dropped.
    for k in &orphans {
        if !contains_session(&root, &k.session_id) {
            root.children.push(leaf(k));
        }
    }

    Ok(Some(JourneyTree {
        journey_id: meta.journey_id,
        root,
    }))
}

/// Recursively collect the children of `parent_sid`, consuming them from the
/// `by_parent` map so they cannot be attached twice.
fn build_children(
    parent_sid: &str,
    by_parent: &mut BTreeMap<String, Vec<MetaKnight>>,
) -> Vec<CascadeNode> {
    let Some(direct) = by_parent.remove(parent_sid) else {
        return Vec::new();
    };
    direct
        .into_iter()
        .map(|k| {
            let sid = k.session_id.clone();
            let mut node = leaf(&k);
            node.children = build_children(&sid, by_parent);
            node
        })
        .collect()
}

fn leaf(k: &MetaKnight) -> CascadeNode {
    CascadeNode {
        session_id: k.session_id.clone(),
        role: k.role.clone(),
        agent: k.agent.clone(),
        tmux_session: k.tmux_session.clone(),
        level: k.level,
        children: Vec::new(),
    }
}

fn contains_session(node: &CascadeNode, sid: &str) -> bool {
    node.session_id == sid || node.children.iter().any(|c| contains_session(c, sid))
}

/// Collect every session id in a tree into `set`.
fn collect_sessions(node: &CascadeNode, set: &mut std::collections::HashSet<String>) {
    set.insert(node.session_id.clone());
    for c in &node.children {
        collect_sessions(c, set);
    }
}

/// Read all `journeys/*/meta.json` under `root_dir` into the governed cascade.
/// Individual parse failures are collected and returned alongside the cascade
/// (a bad meta.json must not blank the whole panel).
pub fn load_all(root_dir: &Path) -> (Cascade, Vec<String>) {
    let mut cascade = Cascade::default();
    let mut errors = Vec::new();

    let journeys_dir = root_dir.join("journeys");
    let entries = match std::fs::read_dir(&journeys_dir) {
        Ok(e) => e,
        Err(e) => {
            errors.push(format!("read journeys/: {e}"));
            return (cascade, errors);
        }
    };

    let mut metas: Vec<PathBuf> = Vec::new();
    for entry in entries.flatten() {
        let meta_path = entry.path().join("meta.json");
        if meta_path.is_file() {
            metas.push(meta_path);
        }
    }
    metas.sort();

    for path in metas {
        match parse_journey(&path) {
            Ok(Some(tree)) => {
                collect_sessions(&tree.root, &mut cascade.governed);
                cascade.journeys.push(tree);
            }
            Ok(None) => {}
            Err(e) => errors.push(e),
        }
    }

    cascade.journeys.sort_by(|a, b| a.journey_id.cmp(&b.journey_id));
    (cascade, errors)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn count_nodes(n: &CascadeNode) -> usize {
        1 + n.children.iter().map(count_nodes).sum::<usize>()
    }

    #[test]
    fn new_format_nests_under_parent() {
        let json = r#"{
            "journey_id": "j-new",
            "king_session": "sess_old_king",
            "new_king_session": "sess_king",
            "tree_root": {"role": "king", "session_id": "sess_king"},
            "knights": [
                {"role":"steward","session_id":"sess_steward","parent":"sess_king","level":2},
                {"role":"coder","session_id":"sess_coder","parent":"sess_king","level":2},
                {"role":"specialist","session_id":"sess_spec","parent":"sess_coder","level":3}
            ]
        }"#;
        let meta: JourneyMeta = serde_json::from_str(json).unwrap();
        assert_eq!(meta.root_session().as_deref(), Some("sess_king"));

        let dir = tempdir_with(&[("j-new", json)]);
        let (cascade, errs) = load_all(dir.path());
        assert!(errs.is_empty(), "errors: {errs:?}");
        assert_eq!(cascade.journeys.len(), 1);
        let root = &cascade.journeys[0].root;
        assert_eq!(root.session_id, "sess_king");
        assert_eq!(root.children.len(), 2, "steward + coder under king");
        // specialist nested under coder, not under king.
        let coder = root
            .children
            .iter()
            .find(|c| c.session_id == "sess_coder")
            .unwrap();
        assert_eq!(coder.children.len(), 1);
        assert_eq!(coder.children[0].session_id, "sess_spec");
        assert_eq!(count_nodes(root), 4);
        assert!(cascade.governed.contains("sess_spec"));
    }

    #[test]
    fn old_format_flattens_under_king() {
        let json = r#"{
            "journey_id": "j-old",
            "king_session": "sess_king_old",
            "knights": [
                {"role":"steward","session_id":"sess_a"},
                {"role":"archmaester","session_id":"sess_b"}
            ]
        }"#;
        let dir = tempdir_with(&[("j-old", json)]);
        let (cascade, errs) = load_all(dir.path());
        assert!(errs.is_empty(), "errors: {errs:?}");
        let root = &cascade.journeys[0].root;
        assert_eq!(root.session_id, "sess_king_old");
        assert_eq!(root.children.len(), 2, "both knights flat under king");
        assert!(root.children.iter().all(|c| c.children.is_empty()));
    }

    #[test]
    fn dangling_parent_is_not_dropped() {
        // coder's parent points at a session not present → still rendered.
        let json = r#"{
            "journey_id": "j-dangle",
            "tree_root": {"session_id": "sess_king"},
            "knights": [
                {"role":"coder","session_id":"sess_coder","parent":"sess_ghost","level":2}
            ]
        }"#;
        let dir = tempdir_with(&[("j-dangle", json)]);
        let (cascade, _errs) = load_all(dir.path());
        let root = &cascade.journeys[0].root;
        assert!(
            root.children.iter().any(|c| c.session_id == "sess_coder"),
            "dangling knight must hang under root, never dropped"
        );
    }

    #[test]
    fn disabled_journey_is_filtered_out() {
        let enabled = r#"{
            "journey_id": "j-on",
            "tui": "ENABLED",
            "tree_root": {"session_id": "sess_on_king"},
            "knights": [{"role":"coder","session_id":"sess_on_c","parent":"sess_on_king","level":2}]
        }"#;
        let disabled = r#"{
            "journey_id": "j-off",
            "tui": "DISABLED",
            "tree_root": {"session_id": "sess_off_king"},
            "knights": [{"role":"coder","session_id":"sess_off_c","parent":"sess_off_king","level":2}]
        }"#;
        let missing = r#"{
            "journey_id": "j-missing",
            "king_session": "sess_miss_king",
            "knights": [{"role":"steward","session_id":"sess_miss_s"}]
        }"#;
        let dir = tempdir_with(&[
            ("j-on", enabled),
            ("j-off", disabled),
            ("j-missing", missing),
        ]);
        let (cascade, errs) = load_all(dir.path());
        assert!(errs.is_empty(), "errors: {errs:?}");
        let ids: Vec<&str> = cascade
            .journeys
            .iter()
            .map(|j| j.journey_id.as_str())
            .collect();
        assert!(ids.contains(&"j-on"), "ENABLED journey shown");
        assert!(ids.contains(&"j-missing"), "MISSING tag = ENABLED, shown");
        assert!(!ids.contains(&"j-off"), "DISABLED journey hidden entirely");
        // The disabled journey's sessions are NOT governed (whole subtree gone).
        assert!(!cascade.governed.contains("sess_off_c"));
        assert!(!cascade.governed.contains("sess_off_king"));
    }

    #[test]
    fn tui_tag_is_case_insensitive_and_defaulty() {
        let mk = |tag: &str| {
            format!(
                r#"{{"journey_id":"j","tui":"{tag}","tree_root":{{"session_id":"k"}},"knights":[]}}"#
            )
        };
        assert!(serde_json::from_str::<JourneyMeta>(&mk("ENABLED"))
            .unwrap()
            .is_tui_enabled());
        assert!(serde_json::from_str::<JourneyMeta>(&mk("enabled"))
            .unwrap()
            .is_tui_enabled());
        assert!(!serde_json::from_str::<JourneyMeta>(&mk("DISABLED"))
            .unwrap()
            .is_tui_enabled());
        // Missing tag → enabled.
        let no_tag: JourneyMeta =
            serde_json::from_str(r#"{"journey_id":"j","king_session":"k","knights":[]}"#).unwrap();
        assert!(no_tag.is_tui_enabled());
    }

    // --- tiny temp-dir helper (no external dep) ---
    struct TempDir(PathBuf);
    impl TempDir {
        fn path(&self) -> &Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn tempdir_with(journeys: &[(&str, &str)]) -> TempDir {
        let base = std::env::temp_dir().join(format!(
            "journey-tui-test-{}-{}",
            std::process::id(),
            fastish_id()
        ));
        for (jid, json) in journeys {
            let jdir = base.join("journeys").join(jid);
            std::fs::create_dir_all(&jdir).unwrap();
            std::fs::write(jdir.join("meta.json"), json).unwrap();
        }
        TempDir(base)
    }
    fn fastish_id() -> u128 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    }
}
