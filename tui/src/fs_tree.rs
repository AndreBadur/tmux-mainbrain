//! FOLDER STRUCTURE — a real, lazily-expanded file tree of the motor root,
//! with read-only preview of `.md/.json/.yaml/.py` files.
//!
//! Directories are read on demand (on first expand) so we never walk the whole
//! tree up front. Hidden entries (dotfiles) and heavy build dirs are skipped to
//! keep the panel useful.

use std::path::{Path, PathBuf};

/// Extensions we offer a preview for (per the build order).
const PREVIEWABLE: &[&str] = &["md", "json", "yaml", "yml", "py"];

/// Directory names we never descend into (noise / heavy).
const SKIP_DIRS: &[&str] = &["target", ".git", "node_modules", "__pycache__", ".pytest_cache"];

/// One node in the folder tree.
#[derive(Debug, Clone)]
pub struct FsNode {
    pub path: PathBuf,
    pub name: String,
    pub is_dir: bool,
    pub expanded: bool,
    /// Loaded children (only populated for expanded dirs).
    pub children: Vec<FsNode>,
    /// True once a directory's children have been read (avoids re-reading).
    pub loaded: bool,
}

impl FsNode {
    fn new(path: PathBuf, is_dir: bool) -> Self {
        let name = path
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| path.to_string_lossy().into_owned());
        Self {
            path,
            name,
            is_dir,
            expanded: false,
            children: Vec::new(),
            loaded: false,
        }
    }

    /// Whether this file is previewable by extension.
    pub fn is_previewable(&self) -> bool {
        if self.is_dir {
            return false;
        }
        self.path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| PREVIEWABLE.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false)
    }
}

/// The folder tree rooted at `root`, with a flattened visible-row cache.
#[derive(Debug)]
pub struct FsTree {
    pub root: FsNode,
    /// Flattened indices (paths) of currently-visible rows, rebuilt on change.
    visible: Vec<Vec<usize>>,
}

impl FsTree {
    /// Build a tree rooted at `root`, with the root pre-expanded one level.
    pub fn new(root: impl AsRef<Path>) -> Self {
        let mut root_node = FsNode::new(root.as_ref().to_path_buf(), true);
        root_node.expanded = true;
        load_children(&mut root_node);
        let mut tree = Self {
            root: root_node,
            visible: Vec::new(),
        };
        tree.rebuild_visible();
        tree
    }

    /// Number of visible rows.
    pub fn visible_len(&self) -> usize {
        self.visible.len()
    }

    /// Borrow the node at visible row `i`, if any.
    pub fn node_at(&self, i: usize) -> Option<&FsNode> {
        let path = self.visible.get(i)?;
        Some(resolve(&self.root, path))
    }

    /// Depth (indent level) of visible row `i`.
    pub fn depth_at(&self, i: usize) -> u16 {
        self.visible.get(i).map(|p| p.len() as u16).unwrap_or(0)
    }

    /// Toggle expand/collapse of the directory at visible row `i`. Loads
    /// children lazily on first expand. Returns true if anything changed.
    pub fn toggle(&mut self, i: usize) -> bool {
        let Some(path) = self.visible.get(i).cloned() else {
            return false;
        };
        let node = resolve_mut(&mut self.root, &path);
        if !node.is_dir {
            return false;
        }
        node.expanded = !node.expanded;
        if node.expanded && !node.loaded {
            load_children(node);
        }
        self.rebuild_visible();
        true
    }

    /// Rebuild the flattened visible-row list from the current expansion state.
    fn rebuild_visible(&mut self) {
        let mut out = Vec::new();
        // The root's *children* are the top-level visible rows (we don't draw
        // the root itself as a row — the panel border names it).
        for (i, child) in self.root.children.iter().enumerate() {
            flatten(child, vec![i], &mut out);
        }
        self.visible = out;
    }

    /// The absolute path of the node at visible row `i`, if any.
    pub fn path_at(&self, i: usize) -> Option<PathBuf> {
        self.node_at(i).map(|n| n.path.clone())
    }

    /// The visible-row index whose node has exactly `path`, if it is currently
    /// visible.
    pub fn index_of_path(&self, path: &Path) -> Option<usize> {
        (0..self.visible.len()).find(|&i| self.node_at(i).map(|n| n.path == path).unwrap_or(false))
    }

    /// Force a full re-scan of the tree from disk, PRESERVING the set of
    /// expanded directory paths (Option B forced reload). Selection is a
    /// caller concern (they capture the selected path before and restore it via
    /// [`index_of_path`] after). Directories that vanished are simply gone;
    /// newly-created files/dirs appear.
    pub fn reload(&mut self) {
        // 1) Snapshot the currently-expanded directory paths.
        let mut expanded: Vec<PathBuf> = Vec::new();
        collect_expanded(&self.root, &mut expanded);

        // 2) Rebuild the root fresh from disk.
        let mut root_node = FsNode::new(self.root.path.clone(), true);
        root_node.expanded = true;
        load_children(&mut root_node);
        self.root = root_node;

        // 3) Re-expand the previously-expanded paths that still exist, loading
        //    their children (deeper paths re-expand as we descend).
        //    Sort by depth so parents expand before children.
        expanded.sort_by_key(|p| p.components().count());
        for path in expanded {
            reexpand_path(&mut self.root, &path);
        }

        self.rebuild_visible();
    }
}

/// Collect the paths of all currently-expanded directories (excluding the root,
/// which is always expanded and rebuilt explicitly).
fn collect_expanded(node: &FsNode, out: &mut Vec<PathBuf>) {
    for child in &node.children {
        if child.is_dir && child.expanded {
            out.push(child.path.clone());
            collect_expanded(child, out);
        }
    }
}

/// Walk from the root to the node whose path == `target` and, if found and it is
/// a still-existing directory, expand + load it. No-op if the path vanished.
fn reexpand_path(root: &mut FsNode, target: &Path) {
    // Find the direct child on the way to `target`, descend, and expand the
    // matching directory node.
    fn walk(node: &mut FsNode, target: &Path) {
        for child in node.children.iter_mut() {
            if child.path == target {
                if child.is_dir {
                    child.expanded = true;
                    if !child.loaded {
                        load_children(child);
                    }
                }
                return;
            }
            if child.is_dir && target.starts_with(&child.path) {
                // Ensure the intermediate dir is loaded so we can descend.
                if !child.loaded {
                    load_children(child);
                }
                walk(child, target);
                return;
            }
        }
    }
    walk(root, target);
}

fn flatten(node: &FsNode, path: Vec<usize>, out: &mut Vec<Vec<usize>>) {
    out.push(path.clone());
    if node.is_dir && node.expanded {
        for (i, child) in node.children.iter().enumerate() {
            let mut p = path.clone();
            p.push(i);
            flatten(child, p, out);
        }
    }
}

/// Resolve a child-index path to a node reference (path is relative to root's
/// children, e.g. `[2, 0]` = root.children[2].children[0]).
fn resolve<'a>(root: &'a FsNode, path: &[usize]) -> &'a FsNode {
    let mut node = &root.children[path[0]];
    for &i in &path[1..] {
        node = &node.children[i];
    }
    node
}

fn resolve_mut<'a>(root: &'a mut FsNode, path: &[usize]) -> &'a mut FsNode {
    let mut node = &mut root.children[path[0]];
    for &i in &path[1..] {
        node = &mut node.children[i];
    }
    node
}

/// Read a directory's immediate children (dirs first, then files, alpha),
/// skipping hidden and heavy dirs. Errors are swallowed to an empty list (a
/// permission error on one dir must not blank the panel).
fn load_children(node: &mut FsNode) {
    node.loaded = true;
    node.children.clear();
    let Ok(entries) = std::fs::read_dir(&node.path) else {
        return;
    };

    let mut dirs: Vec<FsNode> = Vec::new();
    let mut files: Vec<FsNode> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') {
            continue;
        }
        let path = entry.path();
        let is_dir = path.is_dir();
        if is_dir && SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        if is_dir {
            dirs.push(FsNode::new(path, true));
        } else {
            files.push(FsNode::new(path, false));
        }
    }
    dirs.sort_by(|a, b| a.name.cmp(&b.name));
    files.sort_by(|a, b| a.name.cmp(&b.name));
    node.children = dirs;
    node.children.extend(files);
}

/// Read a bounded preview of a file (first `max_bytes`), for the MAIN pane.
/// Returns lossy UTF-8; large files are truncated with a marker.
pub fn preview(path: &Path, max_bytes: usize) -> Result<String, String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; max_bytes + 1];
    let n = f.read(&mut buf).map_err(|e| e.to_string())?;
    let truncated = n > max_bytes;
    let take = n.min(max_bytes);
    let mut text = String::from_utf8_lossy(&buf[..take]).into_owned();
    if truncated {
        text.push_str("\n… (preview truncated)");
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn previewable_by_extension() {
        let md = FsNode::new(PathBuf::from("/x/a.md"), false);
        let py = FsNode::new(PathBuf::from("/x/a.py"), false);
        let bin = FsNode::new(PathBuf::from("/x/a.bin"), false);
        let dir = FsNode::new(PathBuf::from("/x/d"), true);
        assert!(md.is_previewable());
        assert!(py.is_previewable());
        assert!(!bin.is_previewable());
        assert!(!dir.is_previewable(), "dirs are never previewable");
    }

    #[test]
    fn tree_lists_and_toggles() {
        // Build a small temp tree.
        let base = std::env::temp_dir().join(format!(
            "fs-tree-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("a.md"), "# hi").unwrap();
        std::fs::write(base.join("sub").join("b.py"), "print(1)").unwrap();

        let mut tree = FsTree::new(&base);
        // Top level: "sub" (dir) then "a.md" (file).
        assert_eq!(tree.visible_len(), 2);
        assert_eq!(tree.node_at(0).unwrap().name, "sub");
        assert!(tree.node_at(0).unwrap().is_dir);
        assert_eq!(tree.node_at(1).unwrap().name, "a.md");

        // Expand "sub" → its child b.py becomes visible.
        assert!(tree.toggle(0));
        assert_eq!(tree.visible_len(), 3);
        assert_eq!(tree.node_at(1).unwrap().name, "b.py");
        assert_eq!(tree.depth_at(1), 2, "child is one level deeper");

        // Collapse again.
        assert!(tree.toggle(0));
        assert_eq!(tree.visible_len(), 2);

        // Preview the md file.
        let p = base.join("a.md");
        assert_eq!(preview(&p, 100).unwrap(), "# hi");

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn reload_picks_up_new_and_removed_and_keeps_expanded() {
        let base = std::env::temp_dir().join(format!(
            "fs-reload-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("sub").join("b.py"), "print(1)").unwrap();
        std::fs::write(base.join("a.md"), "# hi").unwrap();

        let mut tree = FsTree::new(&base);
        // Expand "sub" so we can verify expansion survives reload.
        let sub_idx = tree.index_of_path(&base.join("sub")).unwrap();
        tree.toggle(sub_idx);
        assert!(tree.index_of_path(&base.join("sub").join("b.py")).is_some());

        // Externally: create a new file and remove a.md.
        std::fs::write(base.join("zzz_new.txt"), "new").unwrap();
        std::fs::remove_file(base.join("a.md")).unwrap();

        tree.reload();

        // New file appears; removed file is gone.
        assert!(
            tree.index_of_path(&base.join("zzz_new.txt")).is_some(),
            "reload picks up the new file"
        );
        assert!(
            tree.index_of_path(&base.join("a.md")).is_none(),
            "reload drops the removed file"
        );
        // Expanded state preserved: sub's child still visible.
        assert!(
            tree.index_of_path(&base.join("sub").join("b.py")).is_some(),
            "expanded 'sub' stays expanded across reload"
        );

        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn preview_rereads_changed_content() {
        let base = std::env::temp_dir().join(format!(
            "fs-preview-reload-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        let f = base.join("note.md");
        std::fs::write(&f, "v1").unwrap();
        assert_eq!(preview(&f, 100).unwrap(), "v1");
        std::fs::write(&f, "v2 changed").unwrap();
        assert_eq!(
            preview(&f, 100).unwrap(),
            "v2 changed",
            "preview re-reads fresh content from disk"
        );
        std::fs::remove_dir_all(&base).ok();
    }
}
