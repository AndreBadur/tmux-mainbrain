//! UI state — kept small and boundary-agnostic. Holds the folder tree, the
//! governed cascade rows, selection/focus, tabs, and the status line. Never
//! talks to the motor directly (background workers feed it via channels).

use std::path::PathBuf;

use crate::cascade::Cascade;
use crate::edit::EditSession;
use crate::fs_tree::{self, FsTree};
use crate::motor::Knight;
use crate::revive::ReviveRequest;
use crate::runtime::{self, LiveTmux};
use crate::tree::{self, TreeRow};
use crate::watch::WatchSession;

/// Which region has keyboard focus. The two left rails are independently
/// focusable; MAIN takes focus when a watch is active so keystrokes route to
/// the embedded pty (and NOT to the rails).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    Folder,
    Subagent,
    /// The MAIN pane — only meaningful while a watch is running there.
    Main,
}

/// What the MAIN pane is currently showing. The left rails are ALWAYS visible;
/// only this MAIN region changes.
pub enum MainView {
    /// Nothing selected yet.
    Empty,
    /// The selected knight's detail (reads `cascade_selected`).
    Knight,
    /// A file preview: (display path, content).
    Preview { path: String, content: String },
    /// A live interactive `tmux attach` rendered INSIDE the MAIN block.
    Watch(Box<WatchSession>),
    /// A live interactive `$EDITOR <file>` rendered INSIDE the MAIN block
    /// (shares the pty widget with Watch). On exit → fresh Preview of the file.
    Edit(Box<EditSession>),
    /// A revive is in progress (resume running on a worker thread).
    Reviving { label: String, text: String },
    /// A knight's TRANSCRIPT (clean turns from `read_turns`) rendered as a
    /// scrollable read-only content view. This is the DURABLE-history path,
    /// complementary to a live Watch: it works even for full-screen alt-screen
    /// knights (kiro-cli/vim) whose live pane keeps no scrollback.
    Transcript { title: String, content: String },
}

/// What `open_watch_for_selected` decided to do — attach directly, kick off a
/// revive, or nothing (with a status message).
pub enum WatchAction {
    /// A watch was opened in MAIN (or focused). Message for the status bar.
    Opened(String),
    /// The selected knight is offline but resumable — caller should spawn the
    /// revive worker with this request.
    Revive(ReviveRequest),
    /// Nothing happened; show this message.
    None(String),
}

/// The whole UI state.
pub struct App {
    /// Motor root (`~/tmux-mainbrain`) — used for the folder tree + previews.
    pub root_dir: PathBuf,
    /// Folder tree (lazily expanded).
    pub folder: FsTree,
    /// Selected visible row in the folder panel.
    pub folder_selected: usize,

    /// The governed cascade (from meta.json), enriched into flat rows.
    pub cascade: Cascade,
    /// Latest live index (scan_knights) for context%/purpose enrichment.
    pub index: Vec<Knight>,
    /// The set of currently-alive tmux session names — AUTHORITY for runtime
    /// presence (glyph + attach-vs-revive decision).
    pub live_tmux: LiveTmux,
    /// Flattened, enriched cascade rows (rebuilt on refresh).
    pub cascade_rows: Vec<TreeRow>,
    /// Selected cascade row (must be a selectable/knight row).
    pub cascade_selected: Option<usize>,
    /// Vertical scroll offset (first visible row) for the SUBAGENT panel — the
    /// cascade list is often longer than its rail, so it scrolls independently.
    /// Selection movement auto-scrolls this to keep the selection visible.
    pub cascade_scroll: usize,
    /// Last-rendered SUBAGENT viewport height (rows) — for clamping/paging the
    /// cascade scroll. Written by the renderer each frame.
    pub cascade_view_h: usize,

    /// Which region has focus.
    pub focus: Focus,
    /// The MAIN pane content.
    pub main: MainView,

    pub status: String,
    pub should_quit: bool,

    /// Hit-test map for mouse: (terminal row y → cascade row index).
    pub cascade_hit: Vec<(u16, usize)>,
    /// Hit-test map for mouse: (terminal row y → folder visible-row index).
    pub folder_hit: Vec<(u16, usize)>,
    /// The MAIN pane's inner rect as (x, y, w, h), for click-to-focus routing.
    pub main_rect: (u16, u16, u16, u16),
    /// The SUBAGENT panel's inner rect as (x, y, w, h), for wheel-scroll routing.
    pub cascade_rect: (u16, u16, u16, u16),
    /// Vertical scroll offset (lines) for scrollable MAIN content views
    /// (Preview / Knight / Reviving). Reset to 0 whenever MAIN content changes.
    pub main_scroll: u16,
    /// Last-rendered content viewport height (body rows) — for clamping scroll.
    pub main_view_h: u16,
    /// Last-rendered total content line count — for clamping scroll.
    pub main_total_lines: u16,
    /// "Stick to bottom" for the current scrollable MAIN view. The scroll
    /// ceiling (`main_total_lines - main_view_h`) is only known AT RENDER time
    /// (the renderer measures the body), so we cannot seed a bottom offset at
    /// `set_main` time. Instead, when true, `render_scrollable` pins the offset
    /// to the real max for that frame and writes it back. Set on transcript
    /// install (open at newest turn); cleared the moment the user scrolls up.
    pub main_stick_bottom: bool,
    /// When true, open watch tabs read-only (`tmux attach -r`) — the D3 fallback.
    pub read_only_watch: bool,
}

impl App {
    pub fn new(root_dir: PathBuf) -> Self {
        let folder = FsTree::new(&root_dir);
        Self {
            root_dir,
            folder,
            folder_selected: 0,
            cascade: Cascade::default(),
            index: Vec::new(),
            live_tmux: LiveTmux::default(),
            cascade_rows: Vec::new(),
            cascade_selected: None,
            cascade_scroll: 0,
            cascade_view_h: 0,
            focus: Focus::Subagent,
            main: MainView::Empty,
            status: default_status(),
            should_quit: false,
            cascade_hit: Vec::new(),
            folder_hit: Vec::new(),
            main_rect: (0, 0, 0, 0),
            cascade_rect: (0, 0, 0, 0),
            main_scroll: 0,
            main_view_h: 0,
            main_total_lines: 0,
            main_stick_bottom: false,
            // Full-interactive by default (D3 ruling); `TUI_READONLY_WATCH=1`
            // flips every watch tab to the read-only `tmux attach -r` fallback.
            read_only_watch: std::env::var("TUI_READONLY_WATCH")
                .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
                .unwrap_or(false),
        }
    }

    /// Replace the governed cascade (from a meta.json reload).
    pub fn update_cascade(&mut self, cascade: Cascade) {
        self.cascade = cascade;
        self.rebuild_cascade_rows();
    }

    /// Replace the MAIN view and reset the scroll offset to the top (any content
    /// change starts unscrolled). All `self.main` reassignments go through this.
    fn set_main(&mut self, view: MainView) {
        self.main = view;
        self.main_scroll = 0;
        // Default every view to top-anchored; the transcript path opts back
        // into stick-to-bottom explicitly (see on_transcript_ready).
        self.main_stick_bottom = false;
    }

    /// Replace the live index (from a scan_knights poll) and re-enrich.
    pub fn update_index(&mut self, index: Vec<Knight>) {
        self.index = index;
        self.rebuild_cascade_rows();
    }

    /// Replace the live tmux set (from a `tmux list-sessions` poll) and
    /// re-enrich — this is what drives the ●/○ glyph correctly.
    pub fn update_tmux(&mut self, live_tmux: LiveTmux) {
        self.live_tmux = live_tmux;
        self.rebuild_cascade_rows();
    }

    /// Rebuild the flattened cascade rows, preserving selection by session id.
    fn rebuild_cascade_rows(&mut self) {
        let prev_sid = self
            .cascade_selected
            .and_then(|i| self.cascade_rows.get(i))
            .and_then(|r| r.session_id.clone());

        self.cascade_rows = tree::build_rows(&self.cascade, &self.index, &self.live_tmux);

        self.cascade_selected = prev_sid.and_then(|sid| {
            self.cascade_rows
                .iter()
                .position(|r| r.session_id.as_deref() == Some(sid.as_str()))
        });

        // Keep the scroll offset valid against the new row count and re-center
        // the (possibly moved) selection so a refresh never hides it.
        let max = self.cascade_scroll_max();
        if self.cascade_scroll > max {
            self.cascade_scroll = max;
        }
        self.cascade_ensure_visible();

        // Keep the MAIN knight view in sync if it was showing a knight (leave a
        // running watch / preview untouched).
        if let MainView::Knight = self.main {
            if self.cascade_selected.is_none() {
                self.set_main(MainView::Empty);
            }
        }
    }

    // --- selection movement (respects current focus) ---

    pub fn select_next(&mut self) {
        match self.focus {
            Focus::Subagent => self.cascade_move(1),
            Focus::Folder => self.folder_move(1),
            // Focus::Main routes keys to the pty (handled upstream); no rail move.
            Focus::Main => {}
        }
    }

    pub fn select_prev(&mut self) {
        match self.focus {
            Focus::Subagent => self.cascade_move(-1),
            Focus::Folder => self.folder_move(-1),
            Focus::Main => {}
        }
    }

    /// Move cascade selection by `delta`, skipping header rows.
    fn cascade_move(&mut self, delta: i32) {
        let n = self.cascade_rows.len();
        if n == 0 {
            self.cascade_selected = None;
            return;
        }
        let mut idx = match self.cascade_selected {
            Some(i) => i as i32,
            None => {
                if delta > 0 {
                    -1
                } else {
                    n as i32
                }
            }
        };
        for _ in 0..n {
            idx += delta;
            if idx < 0 {
                idx = n as i32 - 1;
            } else if idx >= n as i32 {
                idx = 0;
            }
            if self.cascade_rows[idx as usize].is_selectable() {
                self.cascade_selected = Some(idx as usize);
                self.cascade_ensure_visible();
                // Don't clobber a running watch — moving the rail selection just
                // moves the highlight; MAIN keeps the live watch until the user
                // opens a new one (`w`) or closes it (`Ctrl+x c`).
                if !self.main_is_watch() {
                    self.set_main(MainView::Knight);
                }
                return;
            }
        }
    }

    /// Max scroll offset for the SUBAGENT panel given the last-rendered height.
    fn cascade_scroll_max(&self) -> usize {
        self.cascade_rows.len().saturating_sub(self.cascade_view_h)
    }

    /// Auto-scroll the SUBAGENT panel so the current selection is visible: if it
    /// is above the viewport, scroll up to it; if below, scroll down so it sits
    /// on the last visible row. Clamped to the valid scroll range.
    fn cascade_ensure_visible(&mut self) {
        let Some(sel) = self.cascade_selected else {
            return;
        };
        let view_h = self.cascade_view_h;
        if view_h == 0 {
            // Viewport not measured yet — best-effort: put selection at top.
            self.cascade_scroll = sel.min(self.cascade_rows.len());
            return;
        }
        if sel < self.cascade_scroll {
            self.cascade_scroll = sel;
        } else if sel >= self.cascade_scroll + view_h {
            self.cascade_scroll = sel + 1 - view_h;
        }
        let max = self.cascade_scroll_max();
        if self.cascade_scroll > max {
            self.cascade_scroll = max;
        }
    }

    /// Scroll the SUBAGENT panel by `delta` rows (positive = down), clamped.
    /// Used by the mouse wheel over the panel. Selection is left unchanged
    /// (free-scroll); the panel is a browsing surface here.
    pub fn scroll_cascade_lines(&mut self, delta: i32) {
        let max = self.cascade_scroll_max() as i32;
        let next = (self.cascade_scroll as i32 + delta).clamp(0, max);
        self.cascade_scroll = next as usize;
    }

    /// Page the SUBAGENT selection by the viewport height, moving selection to
    /// the next selectable row in that direction and auto-scrolling to it.
    pub fn cascade_page(&mut self, down: bool) {
        let page = self.cascade_view_h.max(1) as i32;
        self.cascade_move(if down { page } else { -page });
    }

    /// Select the FIRST selectable cascade row (Home) and scroll to it.
    pub fn cascade_select_first(&mut self) {
        if let Some(i) = self.cascade_rows.iter().position(|r| r.is_selectable()) {
            self.cascade_selected = Some(i);
            self.cascade_ensure_visible();
            if !self.main_is_watch() {
                self.set_main(MainView::Knight);
            }
        }
    }

    /// Select the LAST selectable cascade row (End) and scroll to it — this is
    /// how the dev reaches a newly-added knight sitting at the bottom.
    pub fn cascade_select_last(&mut self) {
        if let Some(i) = self.cascade_rows.iter().rposition(|r| r.is_selectable()) {
            self.cascade_selected = Some(i);
            self.cascade_ensure_visible();
            if !self.main_is_watch() {
                self.set_main(MainView::Knight);
            }
        }
    }

    fn folder_move(&mut self, delta: i32) {
        let n = self.folder.visible_len();
        if n == 0 {
            return;
        }
        let cur = self.folder_selected as i32;
        let next = (cur + delta).rem_euclid(n as i32) as usize;
        self.folder_selected = next;
    }

    // --- activation (Enter / click) ---

    /// Activate the current selection in the focused panel:
    /// folder dir → toggle; folder file → preview; knight → show detail.
    pub fn activate(&mut self) {
        match self.focus {
            Focus::Folder => self.activate_folder(),
            Focus::Subagent => {
                if self.cascade_selected.is_some() && !self.main_is_watch() {
                    self.set_main(MainView::Knight);
                }
            }
            Focus::Main => {}
        }
    }

    fn activate_folder(&mut self) {
        let i = self.folder_selected;
        let (is_dir, is_prev, path) = match self.folder.node_at(i) {
            Some(node) => (
                node.is_dir,
                node.is_previewable(),
                node.path.clone(),
            ),
            None => return,
        };
        if is_dir {
            self.folder.toggle(i);
        } else if is_prev {
            match fs_tree::preview(&path, 64 * 1024) {
                Ok(content) => {
                    let p = self.display_path(&path);
                    self.set_main(MainView::Preview { path: p, content });
                }
                Err(e) => {
                    self.status = format!("preview error: {e}");
                }
            }
        } else {
            self.status = format!(
                "{} — not previewable (.md/.json/.yaml/.py only)",
                path.file_name()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default()
            );
        }
    }

    fn display_path(&self, path: &std::path::Path) -> String {
        path.strip_prefix(&self.root_dir)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|_| path.to_string_lossy().into_owned())
    }

    /// Cycle focus between the two left-rail panels (Tab key). If MAIN hosts a
    /// live pty OR a scrollable content view, Tab cycles Folder → Subagent →
    /// Main → Folder so the user can Tab into MAIN (to type in a pty, or scroll
    /// a preview) and back out.
    pub fn toggle_focus(&mut self) {
        let main_focusable = self.main_is_watch() || self.main_is_scrollable();
        self.focus = match self.focus {
            Focus::Folder => Focus::Subagent,
            Focus::Subagent => {
                if main_focusable {
                    Focus::Main
                } else {
                    Focus::Folder
                }
            }
            Focus::Main => Focus::Folder,
        };
    }

    // --- mouse routing ---

    /// A left-click at terminal (`x`,`y`). Rail hit maps are checked first;
    /// a click inside the MAIN watch region focuses MAIN (routing keys to the
    /// pty). Returns true if handled.
    pub fn click(&mut self, x: u16, y: u16) -> bool {
        if let Some(&(_, idx)) = self.cascade_hit.iter().find(|(ry, _)| *ry == y) {
            if self
                .cascade_rows
                .get(idx)
                .map(|r| r.is_selectable())
                .unwrap_or(false)
            {
                self.focus = Focus::Subagent;
                self.cascade_selected = Some(idx);
                self.cascade_ensure_visible();
                if !self.main_is_watch() {
                    self.set_main(MainView::Knight);
                }
                return true;
            }
        }
        if let Some(&(_, idx)) = self.folder_hit.iter().find(|(ry, _)| *ry == y) {
            self.focus = Focus::Folder;
            self.folder_selected = idx;
            self.activate_folder();
            return true;
        }
        // Click inside a MAIN pty pane OR a scrollable content view → focus
        // MAIN so keys reach the pty / scroll the content.
        if self.main_is_watch() || self.main_is_scrollable() {
            let (mx, my, mw, mh) = self.main_rect;
            if x >= mx && x < mx + mw && y >= my && y < my + mh {
                self.focus = Focus::Main;
                return true;
            }
        }
        false
    }

    /// The currently-selected knight row, if any.
    pub fn selected_row(&self) -> Option<&TreeRow> {
        self.cascade_selected.and_then(|i| self.cascade_rows.get(i))
    }

    /// Decide what pressing `w` on the selected knight should do — by TMUX
    /// EXISTENCE, NOT motor liveness (the bug fix):
    /// * tmux session EXISTS → ATTACH directly (no resume, no lock check),
    ///   regardless of motor `stale`/`alive`. The normal idle-knight case.
    /// * tmux session ABSENT but a `session_id` exists → REVIVE (resume, with
    ///   the stale-lock guard) — the lock check lives ONLY here.
    /// * no session at all → nothing.
    ///
    /// `rows`x`cols` sizes the pty for the attach path.
    pub fn open_watch_for_selected(&mut self, rows: u16, cols: u16) -> WatchAction {
        let Some(row) = self.selected_row() else {
            return WatchAction::None("no knight selected".to_string());
        };
        let Some(session_id) = row.session_id.clone() else {
            return WatchAction::None("selection has no session id".to_string());
        };
        let label = row.label.clone();
        // Resolve the tmux name (recorded or derived) — SAME resolution the tree
        // uses for the glyph, so decision and display never disagree.
        let tmux = runtime::resolve_tmux_name(row.tmux_session.as_deref(), &label, &session_id);

        // Guard: never attach to our own session (feedback loop).
        if is_own_session(&tmux) {
            return WatchAction::None(format!("refusing to watch own session ({tmux})"));
        }

        // THE DECISION: does the tmux runtime exist right now?
        if self.live_tmux.contains(&tmux) {
            // ATTACH directly — do NOT resume, do NOT check the lock.
            // Already watching this session → just refocus MAIN.
            if let MainView::Watch(w) = &self.main {
                if w.tmux_session == tmux {
                    self.focus = Focus::Main;
                    return WatchAction::Opened(format!("watching {tmux} (focused)"));
                }
            }
            match WatchSession::open(&tmux, rows, cols, self.read_only_watch) {
                Ok(session) => {
                    self.set_main(MainView::Watch(Box::new(session)));
                    self.focus = Focus::Main;
                    let ro = if self.read_only_watch { " (read-only)" } else { "" };
                    WatchAction::Opened(format!(
                        "watching {tmux}{ro} in MAIN — Ctrl+x c to close (detaches)"
                    ))
                }
                Err(e) => WatchAction::None(format!("attach {tmux} failed: {e}")),
            }
        } else {
            // No runtime → REVIVE the existing session_id (context intact). The
            // stale-lock / live-process guard runs inside the revive worker.
            self.set_main(MainView::Reviving {
                label: label.clone(),
                text: format!("no tmux runtime for {tmux} — reviving (resume {session_id})"),
            });
            self.focus = Focus::Subagent;
            WatchAction::Revive(ReviveRequest {
                session_id,
                tmux_session: tmux,
                label,
            })
        }
    }

    /// A revive succeeded — update the meta.json runtime pointer (ONLY
    /// `tmux_session`, never parent/level/role) and attach the live pane in
    /// MAIN. `cleared_lock` notes when a dead-owner stale lock was auto-removed.
    /// `rows`x`cols` sizes the pty. Returns a status message.
    pub fn on_revive_ready(
        &mut self,
        session_id: &str,
        tmux_session: &str,
        cleared_lock: bool,
        rows: u16,
        cols: u16,
    ) -> String {
        // Pointer-only meta update (best-effort; a write failure must not block
        // the attach — the runtime is up regardless).
        let meta_note = match crate::meta_writer::update_tmux_pointer(
            &self.root_dir,
            session_id,
            tmux_session,
        ) {
            Ok(_) => String::new(),
            Err(e) => format!(" (meta pointer not updated: {e})"),
        };

        let lock_note = if cleared_lock {
            "cleared stale lock, "
        } else {
            ""
        };

        match WatchSession::open(tmux_session, rows, cols, self.read_only_watch) {
            Ok(session) => {
                self.set_main(MainView::Watch(Box::new(session)));
                self.focus = Focus::Main;
                format!("{lock_note}resumed {tmux_session} — attached in MAIN{meta_note}")
            }
            Err(e) => {
                let v = if self.cascade_selected.is_some() {
                    MainView::Knight
                } else {
                    MainView::Empty
                };
                self.set_main(v);
                format!("{lock_note}resumed {tmux_session} but attach failed: {e}{meta_note}")
            }
        }
    }

    /// A revive failed/timed out — return MAIN to knight-detail and show why.
    pub fn on_revive_failed(&mut self, reason: &str) {
        let v = if self.cascade_selected.is_some() {
            MainView::Knight
        } else {
            MainView::Empty
        };
        self.set_main(v);
        self.status = reason.to_string();
    }

    /// Update the "reviving" progress text if a revive is showing.
    pub fn on_revive_progress(&mut self, text: &str) {
        if let MainView::Reviving { text: t, .. } = &mut self.main {
            *t = text.to_string();
        }
    }

    /// Begin loading the TRANSCRIPT for the selected knight. Shows a "loading"
    /// Transcript view immediately and returns the `(session_id, title)` the
    /// caller must fetch on a worker thread (non-blocking). Returns `None` (with
    /// a status set) when there is no selectable knight to read.
    pub fn begin_transcript_for_selected(&mut self) -> Option<(String, String)> {
        let Some(row) = self.selected_row() else {
            self.status = "no knight selected".to_string();
            return None;
        };
        let Some(session_id) = row.session_id.clone() else {
            self.status = "selection has no session id".to_string();
            return None;
        };
        let title = row.label.clone();
        self.set_main(MainView::Transcript {
            title: title.clone(),
            content: format!("loading transcript for {title}…"),
        });
        self.focus = Focus::Main;
        Some((session_id, title))
    }

    /// A transcript load finished — render the turns as scrollable content.
    /// `title` is the knight's role/agent label; `content` is the pre-formatted
    /// transcript body. No-op if MAIN moved on to something else meanwhile.
    pub fn on_transcript_ready(&mut self, title: &str, content: String) {
        // Only replace if MAIN is still showing a Transcript (the user may have
        // navigated away while the worker ran).
        if matches!(self.main, MainView::Transcript { .. }) {
            self.set_main(MainView::Transcript {
                title: title.to_string(),
                content,
            });
            // Open anchored at the NEWEST turn. set_main reset scroll to 0; the
            // renderer will pin it to the real bottom on the first frame (the
            // ceiling is only known once the body is measured).
            self.main_stick_bottom = true;
        }
    }

    /// A transcript load failed — surface why and, if MAIN still shows the
    /// loading Transcript, put the error in its body so it is readable.
    pub fn on_transcript_failed(&mut self, title: &str, reason: &str) {
        if matches!(self.main, MainView::Transcript { .. }) {
            self.set_main(MainView::Transcript {
                title: title.to_string(),
                content: format!("failed to load transcript: {reason}"),
            });
        }
        self.status = format!("transcript {title}: {reason}");
    }

    /// Close the MAIN pty view (Watch OR Edit) if one is running: drop it
    /// (Watch::Drop detaches; Edit/PtySession::Drop kills+reaps the editor),
    /// return MAIN to the selected knight's detail / a fresh preview / Empty,
    /// and move focus back to the SUBAGENT rail. Returns true if one closed.
    pub fn close_watch(&mut self) -> bool {
        match &self.main {
            MainView::Watch(w) => {
                let tmux = w.tmux_session.clone();
                let v = self.main_after_pty_close();
                self.set_main(v);
                self.focus = Focus::Subagent;
                self.status = format!("detached {tmux} (knight survives)");
                true
            }
            MainView::Edit(e) => {
                let file = e.file.clone();
                // Editor cancelled via close key → drop (kill+reap) then show a
                // FRESH preview of the file from disk.
                let v = self.preview_or_empty(&file);
                self.set_main(v);
                self.focus = Focus::Folder;
                self.status = format!("closed editor for {}", self.display_path(&file));
                true
            }
            _ => false,
        }
    }

    /// True if MAIN currently shows a SCROLLABLE read-only content view
    /// (Preview / Knight / Reviving) — pty panes (Watch/Edit) scroll via their
    /// own inner app and are excluded.
    pub fn main_is_scrollable(&self) -> bool {
        matches!(
            self.main,
            MainView::Preview { .. }
                | MainView::Knight
                | MainView::Reviving { .. }
                | MainView::Transcript { .. }
        )
    }

    /// Max scroll offset given the last-rendered content height/total.
    fn main_scroll_max(&self) -> u16 {
        self.main_total_lines.saturating_sub(self.main_view_h)
    }

    /// Scroll MAIN content by `delta` lines (positive = down), clamped to
    /// `[0, max]`. No-op if MAIN is not a scrollable content view.
    pub fn scroll_main_lines(&mut self, delta: i32) {
        if !self.main_is_scrollable() {
            return;
        }
        // Scrolling UP means the user wants to read history — stop force-pinning
        // to the bottom and honor their position from here on.
        if delta < 0 {
            self.main_stick_bottom = false;
        }
        let max = self.main_scroll_max() as i32;
        let next = (self.main_scroll as i32 + delta).clamp(0, max);
        self.main_scroll = next as u16;
        // Nice-to-have: reaching the exact bottom re-arms stick-to-bottom so the
        // view keeps following the newest turn.
        if self.main_scroll as i32 == max {
            self.main_stick_bottom = true;
        }
    }

    /// Page up/down by the content viewport height.
    pub fn scroll_main_page(&mut self, down: bool) {
        let page = self.main_view_h.max(1) as i32;
        self.scroll_main_lines(if down { page } else { -page });
    }

    /// Jump to the top of MAIN content.
    pub fn scroll_main_home(&mut self) {
        if self.main_is_scrollable() {
            self.main_scroll = 0;
            // Jumping to the top is an explicit "read history" gesture.
            self.main_stick_bottom = false;
        }
    }

    /// Jump to the bottom of MAIN content.
    pub fn scroll_main_end(&mut self) {
        if self.main_is_scrollable() {
            self.main_scroll = self.main_scroll_max();
        }
    }

    /// True if MAIN currently hosts a live pty (Watch or Edit) — keystrokes
    /// route to it when MAIN is focused.
    pub fn main_is_watch(&self) -> bool {
        matches!(self.main, MainView::Watch(_) | MainView::Edit(_))
    }

    /// True if the MAIN pty is specifically an EDIT session (needs literal
    /// Ctrl+x passthrough for nano-style editors).
    pub fn main_is_edit(&self) -> bool {
        matches!(self.main, MainView::Edit(_))
    }

    /// Take+clear the dirty flag of whichever pty is in MAIN (for the render
    /// throttle). Returns false if MAIN hosts no pty.
    pub fn active_pty_take_dirty(&mut self) -> bool {
        match &mut self.main {
            MainView::Watch(w) => w.take_dirty(),
            MainView::Edit(e) => e.take_dirty(),
            _ => false,
        }
    }

    /// Resize whichever pty is in MAIN to the drawn MAIN inner rect.
    pub fn active_pty_resize(&mut self, rows: u16, cols: u16) {
        match &mut self.main {
            MainView::Watch(w) => w.resize(rows, cols),
            MainView::Edit(e) => e.resize(rows, cols),
            _ => {}
        }
    }

    /// Forward encoded input bytes to whichever pty is in MAIN.
    pub fn active_pty_write_input(&mut self, bytes: &[u8]) {
        match &mut self.main {
            MainView::Watch(w) => w.write_input(bytes),
            MainView::Edit(e) => e.write_input(bytes),
            _ => {}
        }
    }

    /// Apply the active pty's scrollback offset before rendering (so the drawn
    /// screen reflects the history view) and reconcile the tracked offset with
    /// vt100's clamp. No-op if MAIN hosts no pty.
    pub fn active_pty_apply_scrollback(&mut self) {
        match &mut self.main {
            MainView::Watch(w) => w.apply_scrollback(),
            MainView::Edit(e) => e.apply_scrollback(),
            _ => {}
        }
    }

    /// Scroll the active pty's history view by `delta` lines (positive = back).
    pub fn active_pty_scroll_history(&mut self, delta: i32) {
        match &mut self.main {
            MainView::Watch(w) => w.scroll_history(delta),
            MainView::Edit(e) => e.scroll_history(delta),
            _ => {}
        }
    }

    /// Snap the active pty back to live (offset 0). Returns true if it changed
    /// (used to snap before forwarding real typing).
    pub fn active_pty_snap_to_live(&mut self) -> bool {
        match &mut self.main {
            MainView::Watch(w) => w.snap_to_live(),
            MainView::Edit(e) => e.snap_to_live(),
            _ => false,
        }
    }

    /// The active pty's current scrollback offset (0 = live), if any.
    pub fn active_pty_scrollback(&self) -> usize {
        match &self.main {
            MainView::Watch(w) => w.scrollback_offset(),
            MainView::Edit(e) => e.scrollback_offset(),
            _ => 0,
        }
    }

    /// True if the active pty pane is on the ALTERNATE screen (a full-screen
    /// TUI like kiro-cli / vim / less) — such panes keep no scrollback.
    pub fn active_pty_on_alt_screen(&self) -> bool {
        match &self.main {
            MainView::Watch(w) => w.on_alternate_screen(),
            MainView::Edit(e) => e.on_alternate_screen(),
            _ => false,
        }
    }

    /// If the MAIN pty's child has exited — a watched attach ended, OR the
    /// editor was saved+quit — close it. For Edit, MAIN returns to a FRESH
    /// read-only preview (re-read from disk so the user sees their save).
    /// Returns true if a transition happened.
    pub fn reap_dead_watch(&mut self) -> bool {
        // Watch: attach ended.
        let watch_dead = match &mut self.main {
            MainView::Watch(w) => w.has_exited(),
            _ => false,
        };
        if watch_dead {
            if let MainView::Watch(w) = &self.main {
                self.status = format!("{} attach ended — watch closed", w.tmux_session);
            }
            let v = self.main_after_pty_close();
            self.set_main(v);
            self.focus = Focus::Subagent;
            return true;
        }
        // Edit: editor quit → fresh preview from disk.
        let edit_done = match &mut self.main {
            MainView::Edit(e) => e.has_exited(),
            _ => false,
        };
        if edit_done {
            let (file, editor) = match &self.main {
                MainView::Edit(e) => (e.file.clone(), e.editor.clone()),
                _ => unreachable!(),
            };
            // Auto-reload after an edit: the folder tree may have changed (file
            // size/new siblings), and the preview must show the saved content.
            // Re-scan the tree (preserving expanded state + selection by path)
            // BEFORE setting the fresh preview.
            let sel_path = self.folder.path_at(self.folder_selected);
            self.folder.reload();
            self.folder_selected = restore_folder_selection(&self.folder, sel_path.as_deref());
            let v = self.preview_or_empty(&file);
            self.set_main(v);
            self.focus = Focus::Folder;
            self.status = format!(
                "{} closed — {} + folder tree reloaded from disk",
                editor,
                self.display_path(&file)
            );
            return true;
        }
        false
    }

    /// Open the resolved editor on the currently-selected FOLDER file, INSIDE
    /// the MAIN block (King Option 1 — reuse the pty widget). Directories are a
    /// no-op. `rows`x`cols` sizes the editor pty. Returns a status message.
    pub fn open_edit_for_selected(&mut self, rows: u16, cols: u16) -> String {
        let i = self.folder_selected;
        let (is_dir, path) = match self.folder.node_at(i) {
            Some(node) => (node.is_dir, node.path.clone()),
            None => return "no file selected".to_string(),
        };
        if is_dir {
            // Don't edit a directory.
            return "cannot edit a directory".to_string();
        }
        match EditSession::open(&path, rows, cols) {
            Ok(session) => {
                let editor = session.editor.clone();
                self.set_main(MainView::Edit(Box::new(session)));
                self.focus = Focus::Main;
                format!(
                    "editing {} with {editor} — save+quit the editor to return (Ctrl+x c cancels)",
                    self.display_path(&path)
                )
            }
            Err(e) => format!("edit failed: {e}"),
        }
    }

    /// The MAIN view to show after a Watch pty closes: knight-detail if a knight
    /// is selected, else Empty.
    fn main_after_pty_close(&self) -> MainView {
        if self.cascade_selected.is_some() {
            MainView::Knight
        } else {
            MainView::Empty
        }
    }

    /// A fresh read-only preview of `file` from disk, or Empty if unreadable.
    fn preview_or_empty(&self, file: &std::path::Path) -> MainView {
        match fs_tree::preview(file, 64 * 1024) {
            Ok(content) => MainView::Preview {
                path: self.display_path(file),
                content,
            },
            Err(_) => MainView::Empty,
        }
    }

    /// The SHARED forced-reload routine (Option B). Re-scans the FOLDER tree
    /// from disk (preserving expanded state + selection-by-path) and, if MAIN is
    /// showing a file preview, re-reads that file. Does NOT touch a live
    /// Watch/Edit pty. The cascade/tmux immediate refresh is triggered by the
    /// caller (main) poking the poller — this routine is the disk/tree half.
    ///
    /// Called by the `r` key handler (which also pokes the poller for the
    /// cascade/tmux half). The returned message names ALL of what `r` refreshes
    /// — folder + preview + cascade/tmux — so the user is not misled into
    /// thinking only the folder reloaded. Returns a short status message.
    pub fn reload_folder_and_preview(&mut self) -> String {
        // 1) Folder tree: capture selected path, re-scan, restore selection.
        let selected_path = self.folder.path_at(self.folder_selected);
        self.folder.reload();
        self.folder_selected = restore_folder_selection(&self.folder, selected_path.as_deref());

        // 2) Preview: if MAIN shows a file preview, re-read it from disk.
        let preview_path = if let MainView::Preview { path, .. } = &self.main {
            Some(path.clone())
        } else {
            None
        };
        let mut reloaded_preview = false;
        if let Some(path) = preview_path {
            // `path` is display-relative; resolve back to an absolute path.
            let abs = self.root_dir.join(&path);
            if abs.is_file() {
                let v = self.preview_or_empty(&abs);
                self.set_main(v); // resets scroll to top (reload → top is fine)
                reloaded_preview = true;
            }
        }

        if reloaded_preview {
            "reloaded: folder + preview + cascade/tmux".to_string()
        } else {
            "reloaded: folder + cascade/tmux".to_string()
        }
    }
}

/// Restore the folder selection to `path` if it still exists after a reload;
/// else fall back to the nearest still-present ancestor, else clamp to 0.
fn restore_folder_selection(folder: &FsTree, path: Option<&std::path::Path>) -> usize {
    let Some(path) = path else {
        return 0;
    };
    // Exact path still visible → keep the cursor there.
    if let Some(i) = folder.index_of_path(path) {
        return i;
    }
    // Walk up to the nearest ancestor that is still visible.
    let mut cur = path.parent();
    while let Some(anc) = cur {
        if let Some(i) = folder.index_of_path(anc) {
            return i;
        }
        cur = anc.parent();
    }
    // Nothing matched → clamp to the top (or 0 if empty).
    0
}

/// Is `tmux_session` our own session? Compares against `$TUI_OWN_TMUX` and the
/// well-known `coder-tui` name to avoid attaching to ourselves (a feedback
/// loop). This is a safety guard, not a security boundary.
fn is_own_session(tmux_session: &str) -> bool {
    if let Ok(own) = std::env::var("TUI_OWN_TMUX") {
        if own == tmux_session {
            return true;
        }
    }
    tmux_session == "coder-tui"
}

fn default_status() -> String {
    "q quit · ↑/↓ move · Tab focus · Enter preview · e edit · w watch · t transcript · r reload · Ctrl+x c close"
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cascade::{CascadeNode, JourneyTree};
    use crate::motor::Knight;

    fn knight(sid: &str, agent: &str) -> Knight {
        Knight {
            session_id: sid.to_string(),
            agent: Some(agent.to_string()),
            alive: true,
            context_pct: Some(1.0),
            purpose: None,
            parent: None,
        }
    }

    fn cascade(root_sid: &str, child_sids: &[&str]) -> Cascade {
        let children = child_sids
            .iter()
            .map(|s| CascadeNode {
                session_id: s.to_string(),
                role: Some("coder".to_string()),
                agent: None,
                tmux_session: None,
                level: Some(2),
                children: vec![],
            })
            .collect();
        let root = CascadeNode {
            session_id: root_sid.to_string(),
            role: Some("king".to_string()),
            agent: None,
            tmux_session: None,
            level: Some(1),
            children,
        };
        let mut c = Cascade::default();
        c.governed.insert(root_sid.to_string());
        for s in child_sids {
            c.governed.insert(s.to_string());
        }
        c.journeys.push(JourneyTree {
            journey_id: "j".to_string(),
            root,
        });
        c
    }

    #[test]
    fn cascade_move_skips_headers_and_wraps() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(cascade("king", &["a", "b"]));
        app.update_index(vec![knight("king", "wise-king"), knight("a", "x"), knight("b", "y")]);
        // rows: [0 header][1 king][2 a][3 b]
        assert!(app.cascade_rows[0].is_header);
        app.focus = Focus::Subagent;
        app.select_next(); // → king (first selectable)
        assert_eq!(app.cascade_selected, Some(1));
        app.select_next(); // → a
        assert_eq!(app.cascade_selected, Some(2));
        app.select_prev(); // → king
        assert_eq!(app.cascade_selected, Some(1));
    }

    #[test]
    fn selection_survives_index_refresh() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(cascade("king", &["a", "b"]));
        app.update_index(vec![knight("king", "k"), knight("a", "x"), knight("b", "y")]);
        app.focus = Focus::Subagent;
        app.cascade_selected = Some(3); // "b"
        app.main = MainView::Knight;
        // Refresh index only.
        app.update_index(vec![knight("b", "y"), knight("king", "k"), knight("a", "x")]);
        assert_eq!(
            app.cascade_rows[app.cascade_selected.unwrap()]
                .session_id
                .as_deref(),
            Some("b"),
            "selection follows session id across refresh"
        );
    }

    #[test]
    fn focus_toggles_between_rails_when_not_watching() {
        let mut app = App::new(std::env::temp_dir());
        assert_eq!(app.focus, Focus::Subagent);
        app.toggle_focus();
        assert_eq!(app.focus, Focus::Folder);
        app.toggle_focus();
        // No watch running → Subagent cycles back to Folder's partner, never Main.
        assert_eq!(app.focus, Focus::Subagent);
        assert!(!app.main_is_watch());
    }

    #[test]
    fn selecting_knight_sets_knight_view_when_not_watching() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(cascade("king", &["a"]));
        app.update_index(vec![knight("king", "k"), knight("a", "x")]);
        app.focus = Focus::Subagent;
        app.select_next();
        assert!(matches!(app.main, MainView::Knight));
    }

    /// Build a one-knight cascade where the knight has a recorded tmux name.
    fn cascade_with_tmux(sid: &str, tmux: &str) -> Cascade {
        let root = CascadeNode {
            session_id: "root_king".to_string(),
            role: Some("king".to_string()),
            agent: None,
            tmux_session: Some("root-king-tmux".to_string()),
            level: Some(1),
            children: vec![CascadeNode {
                session_id: sid.to_string(),
                role: Some("archmaester".to_string()),
                agent: None,
                tmux_session: Some(tmux.to_string()),
                level: Some(2),
                children: vec![],
            }],
        };
        let mut c = Cascade::default();
        c.governed.insert("root_king".to_string());
        c.governed.insert(sid.to_string());
        c.journeys.push(JourneyTree {
            journey_id: "j".to_string(),
            root,
        });
        c
    }

    #[test]
    fn decision_tmux_exists_takes_attach_path_not_revive() {
        // The exact bug: motor says the knight is NOT alive (stale), but its
        // tmux session EXISTS → pressing w must ATTACH, never REVIVE.
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(cascade_with_tmux("sess_arch", "archmaester-tui"));
        // Motor index marks it stale/offline (alive=false).
        let stale = Knight {
            session_id: "sess_arch".to_string(),
            agent: Some("archmaester".to_string()),
            alive: false,
            context_pct: Some(9.6),
            purpose: None,
            parent: None,
        };
        app.update_index(vec![stale]);
        // tmux says archmaester-tui EXISTS.
        app.update_tmux(LiveTmux::from_names(["archmaester-tui".to_string()]));
        // Select the archmaester row.
        app.focus = Focus::Subagent;
        app.cascade_selected = app
            .cascade_rows
            .iter()
            .position(|r| r.session_id.as_deref() == Some("sess_arch"));
        assert!(app.cascade_selected.is_some());
        // Glyph must be Alive (tmux exists) despite motor stale.
        assert_eq!(app.selected_row().unwrap().state, tree::LiveState::Alive);

        let action = app.open_watch_for_selected(20, 80);
        // MUST NOT be the revive path. (Attach is attempted; on this test host
        // the real attach may fail since we don't run a live tmux, yielding
        // None — but crucially NOT Revive, and MAIN is not Reviving.)
        assert!(
            !matches!(action, WatchAction::Revive(_)),
            "tmux exists → must NOT revive"
        );
        assert!(
            !matches!(app.main, MainView::Reviving { .. }),
            "tmux exists → MAIN must not enter Reviving"
        );
    }

    #[test]
    fn decision_tmux_absent_with_session_takes_revive_path() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(cascade_with_tmux("sess_gone", "gone-tui"));
        app.update_index(vec![]); // not in motor index either
        // tmux set does NOT contain gone-tui.
        app.update_tmux(LiveTmux::from_names(["something-else".to_string()]));
        app.focus = Focus::Subagent;
        app.cascade_selected = app
            .cascade_rows
            .iter()
            .position(|r| r.session_id.as_deref() == Some("sess_gone"));
        assert!(app.cascade_selected.is_some());
        assert_eq!(app.selected_row().unwrap().state, tree::LiveState::Offline);

        let action = app.open_watch_for_selected(20, 80);
        match action {
            WatchAction::Revive(req) => {
                assert_eq!(req.session_id, "sess_gone");
                assert_eq!(req.tmux_session, "gone-tui");
            }
            _ => panic!("tmux absent + session present → must take REVIVE path"),
        }
        assert!(matches!(app.main, MainView::Reviving { .. }));
    }

    #[test]
    fn edit_on_a_directory_is_a_noop() {
        // Build an App rooted at a temp dir whose folder tree has a subdir.
        let base = std::env::temp_dir().join(format!(
            "edit-dir-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(base.join("subdir")).unwrap();
        let mut app = App::new(base.clone());
        // The first visible folder row is "subdir" (a directory).
        app.folder_selected = 0;
        assert!(app.folder.node_at(0).unwrap().is_dir);
        let before = matches!(app.main, MainView::Empty);
        let msg = app.open_edit_for_selected(20, 80);
        assert_eq!(msg, "cannot edit a directory");
        // MAIN unchanged (still Empty), no Edit session opened.
        assert!(before && matches!(app.main, MainView::Empty));
        assert!(!app.main_is_watch());
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn preview_or_empty_reads_fresh_from_disk() {
        let base = std::env::temp_dir().join(format!(
            "edit-preview-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        let file = base.join("note.txt");
        std::fs::write(&file, "line one").unwrap();
        let app = App::new(base.clone());

        // First read.
        match app.preview_or_empty(&file) {
            MainView::Preview { content, .. } => assert_eq!(content, "line one"),
            _ => panic!("expected preview"),
        }
        // Simulate a save, then re-read → fresh content (proves "reload from disk").
        std::fs::write(&file, "line one\nEDITED").unwrap();
        match app.preview_or_empty(&file) {
            MainView::Preview { content, .. } => assert!(content.contains("EDITED")),
            _ => panic!("expected preview"),
        }
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn forced_reload_updates_tree_and_preview_and_keeps_selection() {
        let base = std::env::temp_dir().join(format!(
            "app-reload-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        std::fs::write(base.join("keep.md"), "v1").unwrap();
        std::fs::write(base.join("gone.txt"), "bye").unwrap();
        let mut app = App::new(base.clone());

        // Select keep.md and preview it.
        app.focus = Focus::Folder;
        app.folder_selected = app.folder.index_of_path(&base.join("keep.md")).unwrap();
        app.main = app.preview_or_empty(&base.join("keep.md"));
        assert!(matches!(app.main, MainView::Preview { .. }));

        // Externally: change keep.md, add a new file, remove gone.txt.
        std::fs::write(base.join("keep.md"), "v2 RELOADED").unwrap();
        std::fs::write(base.join("new_after.md"), "n").unwrap();
        std::fs::remove_file(base.join("gone.txt")).unwrap();

        // The SHARED reload routine (same one `r` and auto-triggers call).
        let msg = app.reload_folder_and_preview();
        assert!(msg.contains("reloaded"));

        // Tree reflects add + removal.
        assert!(app.folder.index_of_path(&base.join("new_after.md")).is_some());
        assert!(app.folder.index_of_path(&base.join("gone.txt")).is_none());
        // Selection preserved on keep.md (still exists).
        assert_eq!(
            app.folder.path_at(app.folder_selected).as_deref(),
            Some(base.join("keep.md").as_path())
        );
        // Preview re-read from disk.
        match &app.main {
            MainView::Preview { content, .. } => assert!(content.contains("RELOADED")),
            _ => panic!("expected refreshed preview"),
        }
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn forced_reload_falls_back_to_parent_when_selection_vanishes() {
        let base = std::env::temp_dir().join(format!(
            "app-reload-fallback-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(base.join("sub")).unwrap();
        std::fs::write(base.join("sub").join("child.txt"), "x").unwrap();
        let mut app = App::new(base.clone());
        // Expand sub, select the child.
        let sub_i = app.folder.index_of_path(&base.join("sub")).unwrap();
        app.folder.toggle(sub_i);
        app.folder_selected = app
            .folder
            .index_of_path(&base.join("sub").join("child.txt"))
            .unwrap();
        // Remove the selected child externally.
        std::fs::remove_file(base.join("sub").join("child.txt")).unwrap();
        app.reload_folder_and_preview();
        // Selection falls back to the nearest surviving ancestor ("sub").
        assert_eq!(
            app.folder.path_at(app.folder_selected).as_deref(),
            Some(base.join("sub").as_path()),
            "vanished selection falls back to parent dir"
        );
        std::fs::remove_dir_all(&base).ok();
    }

    // --- MAIN scroll tests ---

    /// Simulate what the renderer records so scroll clamping has real bounds.
    fn set_viewport(app: &mut App, view_h: u16, total: u16) {
        app.main_view_h = view_h;
        app.main_total_lines = total;
    }

    #[test]
    fn scroll_clamps_at_top_and_bottom() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Preview {
            path: "x".to_string(),
            content: "long".to_string(),
        };
        // 100 lines of content, 10-row viewport → max offset 90.
        set_viewport(&mut app, 10, 100);
        app.focus = Focus::Main;

        app.scroll_main_lines(-5); // can't go above 0
        assert_eq!(app.main_scroll, 0);

        app.scroll_main_lines(30);
        assert_eq!(app.main_scroll, 30);

        app.scroll_main_end();
        assert_eq!(app.main_scroll, 90, "End jumps to max = total - viewport");

        app.scroll_main_lines(50); // clamps at max
        assert_eq!(app.main_scroll, 90);

        app.scroll_main_home();
        assert_eq!(app.main_scroll, 0);
    }

    #[test]
    fn scroll_page_moves_by_viewport_height() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Preview {
            path: "x".to_string(),
            content: "c".to_string(),
        };
        set_viewport(&mut app, 20, 200);
        app.scroll_main_page(true);
        assert_eq!(app.main_scroll, 20);
        app.scroll_main_page(true);
        assert_eq!(app.main_scroll, 40);
        app.scroll_main_page(false);
        assert_eq!(app.main_scroll, 20);
    }

    #[test]
    fn scroll_offset_resets_on_content_change() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Preview {
            path: "a".to_string(),
            content: "c".to_string(),
        };
        set_viewport(&mut app, 10, 100);
        app.scroll_main_lines(40);
        assert_eq!(app.main_scroll, 40);
        // Switching MAIN content via set_main resets scroll to top.
        app.set_main(MainView::Preview {
            path: "b".to_string(),
            content: "c2".to_string(),
        });
        assert_eq!(app.main_scroll, 0, "new content starts unscrolled");
    }

    #[test]
    fn scroll_is_noop_for_pty_and_when_not_scrollable() {
        let mut app = App::new(std::env::temp_dir());
        // Empty is not a scrollable content view (only Preview/Knight/Reviving).
        app.main = MainView::Empty;
        set_viewport(&mut app, 10, 100);
        app.scroll_main_lines(20);
        assert_eq!(app.main_scroll, 0, "Empty view does not scroll");
        assert!(!app.main_is_scrollable());
    }

    #[test]
    fn knight_and_reviving_are_scrollable_watch_is_not() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Knight;
        assert!(app.main_is_scrollable());
        app.main = MainView::Reviving {
            label: "l".to_string(),
            text: "t".to_string(),
        };
        assert!(app.main_is_scrollable());
        // (Watch/Edit require a live pty to construct; their exclusion is
        // covered by `main_is_scrollable` matching only the 3 content variants.)
    }

    #[test]
    fn transcript_view_is_scrollable() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Transcript {
            title: "coder".to_string(),
            content: "── ASSISTANT ──\nhello".to_string(),
        };
        assert!(app.main_is_scrollable());
        // It scrolls like the other content views.
        set_viewport(&mut app, 5, 50);
        app.focus = Focus::Main;
        app.scroll_main_lines(20);
        assert_eq!(app.main_scroll, 20);
        app.scroll_main_end();
        assert_eq!(app.main_scroll, 45, "End jumps to total - viewport");
    }

    #[test]
    fn on_transcript_ready_replaces_loading_body() {
        let mut app = App::new(std::env::temp_dir());
        // Simulate begin_transcript setting a loading view.
        app.main = MainView::Transcript {
            title: "coder".to_string(),
            content: "loading…".to_string(),
        };
        app.on_transcript_ready("coder", "── USER ──\nhi".to_string());
        match &app.main {
            MainView::Transcript { title, content } => {
                assert_eq!(title, "coder");
                assert!(content.contains("hi"));
            }
            _ => panic!("expected transcript view"),
        }
    }

    #[test]
    fn transcript_opens_stuck_to_bottom_and_render_seeds_max_offset() {
        // on_transcript_ready arms stick-to-bottom; the renderer (simulated by
        // set_viewport-then-clamp) must pin the offset to the real max.
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Transcript {
            title: "king".to_string(),
            content: "loading…".to_string(),
        };
        app.on_transcript_ready("king", "many\nlines\nof\ntranscript".to_string());
        assert!(app.main_stick_bottom, "transcript opens stuck to bottom");
        // set_main reset scroll to 0; the render-time correction seeds max.
        assert_eq!(app.main_scroll, 0, "scroll starts at 0 until first render");

        // Simulate render_scrollable's stick-to-bottom seeding: 100 lines, 10
        // viewport → max offset 90.
        app.main_view_h = 10;
        app.main_total_lines = 100;
        let max = app.main_total_lines.saturating_sub(app.main_view_h);
        if app.main_stick_bottom {
            app.main_scroll = max;
        }
        assert_eq!(
            app.main_scroll, 90,
            "stick-to-bottom pins offset to the max"
        );
    }

    #[test]
    fn scrolling_up_clears_stick_to_bottom() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Transcript {
            title: "king".to_string(),
            content: "c".to_string(),
        };
        set_viewport(&mut app, 10, 100);
        app.main_stick_bottom = true;
        app.main_scroll = 90; // at the bottom

        // Scroll up → flag clears and offset moves off the bottom.
        app.scroll_main_lines(-5);
        assert!(!app.main_stick_bottom, "scroll-up releases stick-to-bottom");
        assert_eq!(app.main_scroll, 85);

        // Home also clears it (explicit "read history").
        app.main_stick_bottom = true;
        app.scroll_main_home();
        assert!(!app.main_stick_bottom, "Home releases stick-to-bottom");
        assert_eq!(app.main_scroll, 0);
    }

    #[test]
    fn scrolling_back_to_exact_bottom_rearms_stick() {
        let mut app = App::new(std::env::temp_dir());
        app.main = MainView::Transcript {
            title: "king".to_string(),
            content: "c".to_string(),
        };
        set_viewport(&mut app, 10, 100); // max = 90
        app.main_scroll = 80;
        app.main_stick_bottom = false;
        // Scroll down to the exact bottom → re-arm.
        app.scroll_main_lines(10);
        assert_eq!(app.main_scroll, 90);
        assert!(app.main_stick_bottom, "reaching the bottom re-arms stick");
    }

    #[test]
    fn non_transcript_views_do_not_stick_to_bottom() {
        // Preview/Knight/Reviving must stay TOP-anchored: set_main leaves the
        // flag false, so the renderer never pins them to the bottom.
        let mut app = App::new(std::env::temp_dir());
        app.set_main(MainView::Preview {
            path: "p".to_string(),
            content: "long\ncontent".to_string(),
        });
        assert!(!app.main_stick_bottom, "preview opens top-anchored");
        assert_eq!(app.main_scroll, 0);

        app.set_main(MainView::Knight);
        assert!(!app.main_stick_bottom, "knight opens top-anchored");

        app.set_main(MainView::Reviving {
            label: "l".to_string(),
            text: "t".to_string(),
        });
        assert!(!app.main_stick_bottom, "reviving opens top-anchored");
    }

    #[test]
    fn on_transcript_ready_is_noop_when_main_moved_on() {
        let mut app = App::new(std::env::temp_dir());
        // MAIN is NOT a transcript (user navigated away) → ready must not clobber.
        app.main = MainView::Knight;
        app.on_transcript_ready("coder", "body".to_string());
        assert!(
            matches!(app.main, MainView::Knight),
            "ready is a no-op off-transcript"
        );
    }

    // --- FIX 1: SUBAGENT cascade scroll ---

    /// A cascade with one King and `n` level-2 children (all selectable).
    fn wide_cascade(n: usize) -> Cascade {
        let sids: Vec<String> = (0..n).map(|i| format!("child{i}")).collect();
        let refs: Vec<&str> = sids.iter().map(|s| s.as_str()).collect();
        cascade("king", &refs)
    }

    #[test]
    fn cascade_selection_reaches_last_row_and_autoscrolls() {
        let mut app = App::new(std::env::temp_dir());
        // 1 header + king + 8 children = 10 rows; viewport of 4.
        app.update_cascade(wide_cascade(8));
        app.cascade_view_h = 4;
        app.focus = Focus::Subagent;

        // End jumps to the LAST selectable row (the 8th child) and scrolls it
        // into view — this is how a newly-added bottom knight is reached.
        app.cascade_select_last();
        let last = app.cascade_selected.unwrap();
        assert_eq!(
            app.cascade_rows[last].session_id.as_deref(),
            Some("child7"),
            "End reaches the final knight"
        );
        // The selected row must be within the visible window [start, start+h).
        let start = app.cascade_scroll;
        assert!(
            last >= start && last < start + app.cascade_view_h,
            "auto-scroll keeps the last selection visible (start={start}, sel={last})"
        );
        // Home returns to the first selectable row (the King at index 1; the
        // journey header at index 0 is not selectable) and scrolls it into view.
        app.cascade_select_first();
        let first = app.cascade_selected.unwrap();
        assert_eq!(
            app.cascade_rows[first].session_id.as_deref(),
            Some("king"),
            "Home reaches the first knight (the King)"
        );
        let start = app.cascade_scroll;
        assert!(
            first >= start && first < start + app.cascade_view_h,
            "auto-scroll keeps the first selection visible"
        );
    }

    #[test]
    fn cascade_scroll_clamps_at_bounds() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(wide_cascade(8)); // 10 rows total
        app.cascade_view_h = 4; // max start = 10 - 4 = 6

        app.scroll_cascade_lines(-5); // can't go above 0
        assert_eq!(app.cascade_scroll, 0);

        app.scroll_cascade_lines(100); // clamps at max start
        assert_eq!(app.cascade_scroll, 6);

        app.scroll_cascade_lines(-2);
        assert_eq!(app.cascade_scroll, 4);
    }

    #[test]
    fn cascade_scroll_survives_shrinking_refresh() {
        let mut app = App::new(std::env::temp_dir());
        app.update_cascade(wide_cascade(8)); // 10 rows
        app.cascade_view_h = 4;
        app.scroll_cascade_lines(6); // at max
        assert_eq!(app.cascade_scroll, 6);
        // Cascade shrinks to 3 rows (1 header + king + 1 child) on refresh.
        app.update_cascade(wide_cascade(1));
        // scroll must be clamped so it never points past the shorter list.
        let max = app.cascade_rows.len().saturating_sub(app.cascade_view_h);
        assert!(app.cascade_scroll <= max, "scroll clamped after shrink");
    }

    // --- FIX 2: `r` reload status names the cascade ---

    #[test]
    fn reload_status_names_cascade() {
        let base = std::env::temp_dir().join(format!(
            "app-reload-status-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&base).unwrap();
        let mut app = App::new(base.clone());
        let msg = app.reload_folder_and_preview();
        assert!(
            msg.contains("cascade"),
            "r status must name the cascade, got: {msg}"
        );
        std::fs::remove_dir_all(&base).ok();
    }
}
