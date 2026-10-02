//! journey-tui — a reactive TUI for the tmux-mainbrain subagent cascade.
//!
//! Milestone 2: the SUBAGENT panel renders the GOVERNED cascade tree read from
//! `journeys/*/meta.json` (authoritative L1→L2→L3 command hierarchy), enriched
//! live from `scan_knights`; live-but-ungoverned sessions form an "unlinked"
//! group. The FOLDER panel is a real file tree of the motor root with read-only
//! preview of `.md/.json/.yaml/.py`. Mouse + keyboard select; `q` quits clean.

mod app;
mod cascade;
mod edit;
mod fs_tree;
mod meta_writer;
mod motor;
mod pty;
mod poller;
mod revive;
mod runtime;
mod transcript;
mod tree;
mod ui;
mod watch;

use std::io::{self, Stdout};
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind,
    KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;

use app::{App, Focus, WatchAction};
use motor::MotorClient;
use poller::{PollMsg, PollerHandle};
use revive::ReviveMsg;
use transcript::{TranscriptMsg, TranscriptRequest};

/// Data refresh cadence (index poll + meta.json reload).
const POLL_INTERVAL: Duration = Duration::from_secs(3);
/// Render throttle — cap redraws at ~30fps (D3 budget).
const FRAME_MIN: Duration = Duration::from_millis(33);
/// Event-poll timeout so channel messages stay responsive without busy-waiting.
const EVENT_POLL: Duration = Duration::from_millis(50);

type Tui = Terminal<CrosstermBackend<Stdout>>;

/// Bundle of the shared motor client + worker channels threaded into the key
/// handler (keeps `handle_key`'s signature small — one struct instead of four
/// positional args). Borrowed for the lifetime of a single key dispatch.
struct Workers<'a> {
    client: &'a MotorClient,
    revive_tx: &'a std::sync::mpsc::Sender<ReviveMsg>,
    transcript_tx: &'a std::sync::mpsc::Sender<TranscriptMsg>,
    poller: &'a PollerHandle,
}

fn main() -> Result<()> {
    let motor_cwd = resolve_motor_cwd();
    let mut terminal = setup_terminal().context("failed to set up terminal")?;
    let result = run_app(&mut terminal, motor_cwd);
    restore_terminal(&mut terminal).context("failed to restore terminal")?;
    result
}

fn resolve_motor_cwd() -> PathBuf {
    if let Ok(dir) = std::env::var("TMUX_MAINBRAIN") {
        return PathBuf::from(dir);
    }
    if let Ok(home) = std::env::var("HOME") {
        return PathBuf::from(home).join("tmux-mainbrain");
    }
    PathBuf::from(".")
}

fn setup_terminal() -> Result<Tui> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    // Mouse capture is mandatory (D6).
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    Ok(Terminal::new(CrosstermBackend::new(stdout))?)
}

fn restore_terminal(terminal: &mut Tui) -> Result<()> {
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    Ok(())
}

fn run_app(terminal: &mut Tui, motor_cwd: PathBuf) -> Result<()> {
    let mut app = App::new(motor_cwd.clone());

    let (tx, rx) = mpsc::channel::<PollMsg>();
    let client = MotorClient::new(&motor_cwd);
    let mut poller = PollerHandle::spawn(client.clone(), motor_cwd, POLL_INTERVAL, tx);

    // Revive worker → UI channel (Feature A). The client is cloned per request.
    let (revive_tx, revive_rx) = mpsc::channel::<ReviveMsg>();

    // Transcript worker → UI channel (read_turns history view). Cloned per req.
    let (transcript_tx, transcript_rx) = mpsc::channel::<TranscriptMsg>();

    let mut last_draw = Instant::now().checked_sub(FRAME_MIN).unwrap_or_else(Instant::now);
    let mut dirty = true;
    // Leader-key (`Ctrl+x`) state machine.
    let mut leader = LeaderState::Idle;
    // Last known full terminal area (for sizing the pty on open/resize).
    let mut term_area = terminal.size().map(area_from_size).unwrap_or_default();

    while !app.should_quit {
        while let Ok(msg) = rx.try_recv() {
            match msg {
                PollMsg::Knights(index) => {
                    app.update_index(index);
                    dirty = true;
                }
                PollMsg::Tmux(live_tmux) => {
                    app.update_tmux(live_tmux);
                    dirty = true;
                }
                PollMsg::Cascade(casc) => {
                    app.update_cascade(casc);
                    dirty = true;
                }
                PollMsg::Warn(w) => {
                    app.status = format!("warn: {w}");
                    dirty = true;
                }
                PollMsg::Error(e) => {
                    app.status = format!("motor error: {e}  (q quit)");
                    dirty = true;
                }
            }
        }

        // Drain revive worker messages (Feature A).
        while let Ok(msg) = revive_rx.try_recv() {
            match msg {
                ReviveMsg::Progress { text, .. } => {
                    app.on_revive_progress(&text);
                    dirty = true;
                }
                ReviveMsg::Ready {
                    label,
                    session_id,
                    tmux_session,
                    cleared_lock,
                } => {
                    let region = ui::watch_area(term_area);
                    let msg = app.on_revive_ready(
                        &session_id,
                        &tmux_session,
                        cleared_lock,
                        region.height,
                        region.width,
                    );
                    app.status = format!("{label}: {msg}");
                    // Auto-reload: poke the poller so the revived knight flips to
                    // ● immediately (no up-to-3s wait for the next tick).
                    poller.poke();
                    dirty = true;
                }
                ReviveMsg::Failed { label, reason } => {
                    app.on_revive_failed(&format!("{label}: {reason}"));
                    dirty = true;
                }
            }
        }

        // Drain transcript worker messages (read_turns history view).
        while let Ok(msg) = transcript_rx.try_recv() {
            match msg {
                TranscriptMsg::Ready { title, content } => {
                    app.on_transcript_ready(&title, content);
                    app.status = format!("transcript loaded: {title}");
                    dirty = true;
                }
                TranscriptMsg::Failed { title, reason } => {
                    app.on_transcript_failed(&title, &reason);
                    dirty = true;
                }
            }
        }

        // If the MAIN pty child exited — a watched attach ended, or the editor
        // was saved+quit → transition MAIN (Edit → fresh preview).
        if app.reap_dead_watch() {
            dirty = true;
        }
        if app.active_pty_take_dirty() {
            dirty = true;
        }

        // Keep the MAIN pty (watch OR edit) sized to the drawn MAIN inner rect.
        {
            let region = ui::watch_area(term_area);
            app.active_pty_resize(region.height, region.width);
        }

        if dirty && last_draw.elapsed() >= FRAME_MIN {
            terminal.draw(|f| ui::draw(f, &mut app))?;
            last_draw = Instant::now();
            dirty = false;
        }

        if event::poll(EVENT_POLL)? {
            match event::read()? {
                Event::Key(key) => {
                    let workers = Workers {
                        client: &client,
                        revive_tx: &revive_tx,
                        transcript_tx: &transcript_tx,
                        poller: &poller,
                    };
                    if handle_key(&mut app, key, &mut leader, term_area, &workers) {
                        dirty = true;
                    }
                }
                Event::Mouse(m) => {
                    if handle_mouse(&mut app, m) {
                        dirty = true;
                    }
                }
                Event::Resize(cols, rows) => {
                    term_area = Rect {
                        x: 0,
                        y: 0,
                        width: cols,
                        height: rows,
                    };
                    dirty = true;
                }
                _ => {}
            }
        }
    }

    // Dropping App drops any running MAIN watch → WatchSession::drop detaches
    // cleanly (the knight survives). Stop the poller explicitly.
    poller.stop();
    Ok(())
}

fn area_from_size(s: ratatui::layout::Size) -> Rect {
    Rect {
        x: 0,
        y: 0,
        width: s.width,
        height: s.height,
    }
}

/// Route a key event. Returns true if a redraw is warranted.
///
/// Focus/close routing (Bug-1 fix): `Ctrl+x` is an ALWAYS-armable leader — even
/// inside a focused pty. From a focused pane, the next key means: `c` closes or
/// detaches the pane (WATCH detaches and the knight survives; EDIT cancels back
/// to preview); a second `Ctrl+x` sends a LITERAL Ctrl+x to the child (nano
/// save-quit, vim, etc.); any other key cancels the chord and is forwarded to
/// the child so no keystroke is lost. Off-pty, `Ctrl+x` is the classic TUI
/// leader (c close, w watch, q quit). `Shift+Tab` remains an extra
/// escape-focus affordance.
fn handle_key(
    app: &mut App,
    key: KeyEvent,
    leader: &mut LeaderState,
    term_area: Rect,
    workers: &Workers,
) -> bool {
    if key.kind == KeyEventKind::Release {
        return false;
    }

    // A pty pane (watch OR edit) that currently has focus.
    let pty_focused = app.focus == Focus::Main && app.main_is_watch();
    // A scrollable read-only content view (Preview/Knight/Reviving) with focus.
    let content_focused = app.focus == Focus::Main && app.main_is_scrollable();

    // --- Complete a pending leader chord ---
    if let LeaderState::Armed { from_pty } = *leader {
        *leader = LeaderState::Idle;
        if from_pty {
            return handle_pty_leader_command(app, key);
        }
        return handle_leader_command(app, key, term_area, workers.client, workers.revive_tx);
    }

    // --- Ctrl+x arms the leader in ALL contexts (fix: also inside a pty) ---
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('x') {
        *leader = LeaderState::Armed { from_pty: pty_focused };
        app.status = if pty_focused {
            if app.main_is_edit() {
                "leader: c=close · Ctrl+x=send literal Ctrl+x to editor · other=cancel".to_string()
            } else {
                "leader: c=close/detach watch · Ctrl+x=send literal · other=cancel".to_string()
            }
        } else {
            "leader: c close-watch/edit · w watch selected · q quit".to_string()
        };
        return true;
    }

    // --- Focused pty (no leader pending) ---
    if pty_focused {
        if key.code == KeyCode::BackTab {
            app.toggle_focus();
            app.status =
                "focus left the pane (Ctrl+x c to close, or Tab to re-enter)".to_string();
            return true;
        }
        // Scrollback controls — PageUp/PageDown scroll history, NOT forwarded
        // to the child (they are scroll controls here). One page ≈ the pane
        // height (minus a couple rows for context).
        let page = app.main_rect.3.saturating_sub(2).max(1) as i32;
        match key.code {
            KeyCode::PageUp => {
                // A full-screen TUI (kiro-cli/vim/less) on the ALT screen keeps
                // NO scrollback — be honest instead of faking a counter.
                if app.active_pty_on_alt_screen() {
                    app.status =
                        "no scrollback (full-screen app — scroll inside it)".to_string();
                    return true;
                }
                app.active_pty_scroll_history(page); // back into history (self-clamped)
                let off = app.active_pty_scrollback();
                app.status = if off == 0 {
                    // Requested history but nothing was retained → honest.
                    "no scrollback available (already at oldest / no history)".to_string()
                } else {
                    format!("▲ SCROLLBACK -{off} (PageDown/type = live)")
                };
                return true;
            }
            KeyCode::PageDown => {
                if app.active_pty_on_alt_screen() {
                    app.status =
                        "no scrollback (full-screen app — scroll inside it)".to_string();
                    return true;
                }
                app.active_pty_scroll_history(-page); // toward live
                let off = app.active_pty_scrollback();
                app.status = if off == 0 {
                    "live".to_string()
                } else {
                    format!("▲ SCROLLBACK -{off}")
                };
                return true;
            }
            _ => {}
        }
        // SNAP-TO-LIVE: any real keystroke returns to live BEFORE reaching the
        // child, so the user never types while reviewing history.
        app.active_pty_snap_to_live();
        // Everything else (incl. Ctrl+c interrupt) → the child.
        return forward_key_to_pty(app, key);
    }

    // --- Focused scrollable content (Preview/Knight/Reviving): scroll keys. ---
    if content_focused {
        match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                app.scroll_main_lines(-1);
                return true;
            }
            KeyCode::Down | KeyCode::Char('j') => {
                app.scroll_main_lines(1);
                return true;
            }
            KeyCode::PageUp => {
                app.scroll_main_page(false);
                return true;
            }
            KeyCode::PageDown | KeyCode::Char(' ') => {
                app.scroll_main_page(true);
                return true;
            }
            KeyCode::Home => {
                app.scroll_main_home();
                return true;
            }
            KeyCode::End => {
                app.scroll_main_end();
                return true;
            }
            KeyCode::Tab | KeyCode::BackTab => {
                app.toggle_focus();
                return true;
            }
            KeyCode::Char('q') => {
                app.should_quit = true;
                return true;
            }
            _ => {}
        }
        // fall through to the rail keys below for anything unhandled
    }

    // --- Ctrl+c off-pty quits ---
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        app.should_quit = true;
        return true;
    }

    // --- Rail keys (dashboard) ---
    match key.code {
        KeyCode::Char('q') => {
            app.should_quit = true;
            true
        }
        KeyCode::Down | KeyCode::Char('j') => {
            app.select_next();
            true
        }
        KeyCode::Up | KeyCode::Char('k') => {
            app.select_prev();
            true
        }
        KeyCode::Tab | KeyCode::BackTab => {
            app.toggle_focus();
            true
        }
        KeyCode::Left => {
            app.focus = Focus::Folder;
            true
        }
        KeyCode::Right => {
            app.focus = Focus::Subagent;
            true
        }
        KeyCode::Char('w') => {
            open_watch(app, term_area, workers.client, workers.revive_tx);
            true
        }
        KeyCode::Char('e') => {
            // Edit the selected FOLDER file in MAIN (reuses the pty widget).
            let region = ui::watch_area(term_area);
            app.status = app.open_edit_for_selected(region.height, region.width);
            true
        }
        KeyCode::Char('t') => {
            // Open the selected knight's TRANSCRIPT (read_turns history) in
            // MAIN — the durable-history path, works even for full-screen
            // knights whose live pane keeps no scrollback. Non-blocking.
            if let Some((session_id, title)) = app.begin_transcript_for_selected() {
                app.status = format!("loading transcript for {title}…");
                transcript::spawn(
                    workers.client.clone(),
                    TranscriptRequest { session_id, title },
                    workers.transcript_tx.clone(),
                );
            }
            true
        }
        KeyCode::PageDown if app.focus == Focus::Subagent => {
            app.cascade_page(true);
            true
        }
        KeyCode::PageUp if app.focus == Focus::Subagent => {
            app.cascade_page(false);
            true
        }
        KeyCode::Home if app.focus == Focus::Subagent => {
            app.cascade_select_first();
            true
        }
        KeyCode::End if app.focus == Focus::Subagent => {
            app.cascade_select_last();
            true
        }
        KeyCode::Char('r') => {
            // Forced reload (Option B): re-scan folder tree + re-read preview,
            // then poke the poller so the cascade/tmux refreshes NOW.
            let msg = app.reload_folder_and_preview();
            workers.poller.poke();
            app.status = msg;
            true
        }
        KeyCode::Enter | KeyCode::Char(' ') => {
            app.activate();
            true
        }
        _ => false,
    }
}

/// The leader-key (`Ctrl+x`) state machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LeaderState {
    /// No chord in progress.
    Idle,
    /// `Ctrl+x` pressed; awaiting the command key. `from_pty` records whether it
    /// was armed from inside a focused pty (changes the command set + literal
    /// passthrough).
    Armed { from_pty: bool },
}

/// What a key pressed after `Ctrl+x` inside a focused pty resolves to. Split
/// out as a pure function so the routing is unit-testable without a live pty.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PtyLeaderAction {
    /// `c` → close/detach the pane.
    Close,
    /// `Ctrl+x` → send a literal Ctrl+x (0x18) to the child.
    SendLiteralCtrlX,
    /// anything else → cancel the chord and forward the key to the child.
    CancelAndForward,
}

/// Classify the command key of a focused-pty `Ctrl+x` chord.
fn classify_pty_leader(key: KeyEvent) -> PtyLeaderAction {
    match key.code {
        KeyCode::Char('c') => PtyLeaderAction::Close,
        KeyCode::Char('x') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            PtyLeaderAction::SendLiteralCtrlX
        }
        _ => PtyLeaderAction::CancelAndForward,
    }
}

/// Handle the key after `Ctrl+x` was pressed INSIDE a focused pty pane: `c`
/// closes/detaches the pane; a second `Ctrl+x` sends a LITERAL Ctrl+x (byte
/// 0x18) to the child (nano etc.); anything else cancels and forwards the key.
fn handle_pty_leader_command(app: &mut App, key: KeyEvent) -> bool {
    match classify_pty_leader(key) {
        PtyLeaderAction::Close => {
            app.close_watch();
            true
        }
        PtyLeaderAction::SendLiteralCtrlX => {
            app.active_pty_write_input(&[0x18]);
            app.status = "sent Ctrl+x to the editor".to_string();
            true
        }
        PtyLeaderAction::CancelAndForward => {
            app.status = "leader cancelled".to_string();
            forward_key_to_pty(app, key);
            true
        }
    }
}

/// Handle the key after the `Ctrl+x` leader.
fn handle_leader_command(
    app: &mut App,
    key: KeyEvent,
    term_area: Rect,
    client: &MotorClient,
    revive_tx: &std::sync::mpsc::Sender<ReviveMsg>,
) -> bool {
    match key.code {
        KeyCode::Char('c') => {
            app.close_watch();
            true
        }
        KeyCode::Char('w') => {
            open_watch(app, term_area, client, revive_tx);
            true
        }
        KeyCode::Char('q') => {
            app.should_quit = true;
            true
        }
        _ => {
            app.status = "leader cancelled".to_string();
            true
        }
    }
}

/// Open (or switch) the MAIN watch to the selected knight, sized to the MAIN
/// inner rect. If the knight is offline-but-resumable, kick off a background
/// revive (resume the existing session, then attach) instead of failing.
fn open_watch(
    app: &mut App,
    term_area: Rect,
    client: &MotorClient,
    revive_tx: &std::sync::mpsc::Sender<ReviveMsg>,
) {
    let region = ui::watch_area(term_area);
    match app.open_watch_for_selected(region.height, region.width) {
        WatchAction::Opened(msg) => app.status = msg,
        WatchAction::None(msg) => app.status = msg,
        WatchAction::Revive(req) => {
            app.status = format!("reviving {}…", req.tmux_session);
            revive::spawn(client.clone(), req, revive_tx.clone());
        }
    }
}

/// Encode a crossterm key to terminal bytes and write to the MAIN pty (watch
/// OR edit — both host a pty; input routes to whichever is active).
fn forward_key_to_pty(app: &mut App, key: KeyEvent) -> bool {
    let Some(bytes) = encode_key(key) else {
        return false;
    };
    app.active_pty_write_input(&bytes);
    // The pty echo drives the redraw via the dirty flag; no forced redraw.
    false
}

/// Translate a crossterm [`KeyEvent`] into the byte sequence a terminal would
/// send. Covers the common set; exotic combos (Ctrl+Arrow, Alt+Backspace) are a
/// documented follow-up (D3 keymap-pass caveat).
fn encode_key(key: KeyEvent) -> Option<Vec<u8>> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);

    let mut out: Vec<u8> = Vec::new();
    if alt {
        out.push(0x1b); // Alt prefixes with ESC.
    }

    match key.code {
        KeyCode::Char(c) => {
            if ctrl {
                let b = c.to_ascii_lowercase() as u8;
                let ctrl_byte = match b {
                    b'a'..=b'z' => b - b'a' + 1,
                    b' ' | b'@' => 0,
                    b'[' => 0x1b,
                    b'\\' => 0x1c,
                    b']' => 0x1d,
                    b'^' => 0x1e,
                    b'_' => 0x1f,
                    _ => return if alt { Some(out) } else { None },
                };
                out.push(ctrl_byte);
            } else {
                let mut buf = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            }
        }
        KeyCode::Enter => out.push(b'\r'),
        KeyCode::Tab => out.push(b'\t'),
        KeyCode::BackTab => out.extend_from_slice(b"\x1b[Z"),
        KeyCode::Backspace => out.push(0x7f),
        KeyCode::Esc => out.push(0x1b),
        KeyCode::Up => out.extend_from_slice(b"\x1b[A"),
        KeyCode::Down => out.extend_from_slice(b"\x1b[B"),
        KeyCode::Right => out.extend_from_slice(b"\x1b[C"),
        KeyCode::Left => out.extend_from_slice(b"\x1b[D"),
        KeyCode::Home => out.extend_from_slice(b"\x1b[H"),
        KeyCode::End => out.extend_from_slice(b"\x1b[F"),
        KeyCode::PageUp => out.extend_from_slice(b"\x1b[5~"),
        KeyCode::PageDown => out.extend_from_slice(b"\x1b[6~"),
        KeyCode::Delete => out.extend_from_slice(b"\x1b[3~"),
        KeyCode::Insert => out.extend_from_slice(b"\x1b[2~"),
        KeyCode::F(n) => {
            let seq: &[u8] = match n {
                1 => b"\x1bOP",
                2 => b"\x1bOQ",
                3 => b"\x1bOR",
                4 => b"\x1bOS",
                5 => b"\x1b[15~",
                6 => b"\x1b[17~",
                7 => b"\x1b[18~",
                8 => b"\x1b[19~",
                9 => b"\x1b[20~",
                10 => b"\x1b[21~",
                11 => b"\x1b[23~",
                12 => b"\x1b[24~",
                _ => return None,
            };
            out.extend_from_slice(seq);
        }
        _ => return None,
    }
    Some(out)
}

fn handle_mouse(app: &mut App, m: MouseEvent) -> bool {
    match m.kind {
        MouseEventKind::Down(MouseButton::Left) => app.click(m.column, m.row),
        MouseEventKind::ScrollDown => scroll_main_wheel(app, m.column, m.row, 3),
        MouseEventKind::ScrollUp => scroll_main_wheel(app, m.column, m.row, -3),
        _ => false,
    }
}

/// Route a wheel event: over a pty pane → scroll its scrollback history; over a
/// scrollable content view → scroll the content. `delta` is content-scroll
/// direction (positive = down); pty history uses the inverse (up = back).
fn scroll_main_wheel(app: &mut App, x: u16, y: u16, delta: i32) -> bool {
    // Wheel over the SUBAGENT panel scrolls the cascade list independently.
    let (cx, cy, cw, ch) = app.cascade_rect;
    if cw > 0 && x >= cx && x < cx + cw && y >= cy && y < cy + ch {
        app.scroll_cascade_lines(delta);
        return true;
    }

    let (mx, my, mw, mh) = app.main_rect;
    let over_main = x >= mx && x < mx + mw && y >= my && y < my + mh;

    // PTY pane (Watch/Edit): wheel scrolls the vt100 scrollback.
    if app.main_is_watch() {
        if over_main || app.focus == Focus::Main {
            // Wheel up (delta<0) → back into history (+); wheel down → toward live.
            app.active_pty_scroll_history(-delta);
            return true;
        }
        return false;
    }

    // Scrollable content view (Preview/Knight/Reviving).
    if app.main_is_scrollable() && (over_main || app.focus == Focus::Main) {
        app.scroll_main_lines(delta);
        return true;
    }
    false
}


#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode, ctrl: bool) -> KeyEvent {
        let mods = if ctrl {
            KeyModifiers::CONTROL
        } else {
            KeyModifiers::NONE
        };
        KeyEvent::new(code, mods)
    }

    #[test]
    fn focused_pty_ctrl_x_then_c_closes() {
        // `Ctrl+x` then `c` inside a focused pty → Close (used by WATCH to
        // detach and by EDIT to cancel).
        assert_eq!(
            classify_pty_leader(key(KeyCode::Char('c'), false)),
            PtyLeaderAction::Close
        );
    }

    #[test]
    fn focused_pty_ctrl_x_ctrl_x_sends_literal() {
        // `Ctrl+x` then `Ctrl+x` → literal Ctrl+x to the child (nano save-quit).
        assert_eq!(
            classify_pty_leader(key(KeyCode::Char('x'), true)),
            PtyLeaderAction::SendLiteralCtrlX
        );
    }

    #[test]
    fn focused_pty_ctrl_x_other_key_cancels_and_forwards() {
        // Any other key cancels the chord and is forwarded (no keystroke lost).
        assert_eq!(
            classify_pty_leader(key(KeyCode::Char('z'), false)),
            PtyLeaderAction::CancelAndForward
        );
        // A bare 'x' (no ctrl) is NOT the literal-passthrough; it cancels.
        assert_eq!(
            classify_pty_leader(key(KeyCode::Char('x'), false)),
            PtyLeaderAction::CancelAndForward
        );
    }

    #[test]
    fn literal_ctrl_x_byte_is_0x18() {
        // The literal Ctrl+x control byte (nano's expected input) is 0x18.
        assert_eq!(
            encode_key(key(KeyCode::Char('x'), true)),
            Some(vec![0x18])
        );
    }
}
