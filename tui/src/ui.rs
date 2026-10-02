//! Rendering — draws the sketch and records mouse hit-test maps.
//!
//! ```text
//! ┌ title ────────────────────────────────────────────────┐
//! │ FOLDER STRUCTURE │                                     │
//! │  (file tree)     │              MAIN pane              │
//! ├──────────────────┤  (detail · preview · LIVE watch)    │
//! │ SUBAGENT STRUCT. │                                     │
//! │  (cascade tree)  │                                     │
//! ├──────────────────┴──────────────────────────────────────┤
//! │ status / keybinds                                        │
//! └──────────────────────────────────────────────────────────┘
//! ```
//!
//! The left rails are ALWAYS visible; only the MAIN region changes (including
//! the live interactive watch). Rendering is pure over [`App`] except it
//! records hit-test rects back into it.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use crate::app::{App, Focus, MainView};
use crate::tree::{LiveState, TreeRow};

/// The two-column body layout: [rails (38) | MAIN (rest)].
fn body_columns(area: Rect) -> [Rect; 2] {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(38), Constraint::Min(20)])
        .split(area);
    [cols[0], cols[1]]
}

/// The full body rect (between the title bar and the status bar).
fn body_rect(area: Rect) -> Rect {
    Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(3),
            Constraint::Length(1),
        ])
        .split(area)[1]
}

pub fn draw(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(3),
            Constraint::Length(1),
        ])
        .split(area);

    draw_title(f, app, rows[0]);
    draw_body(f, app, rows[1]);
    draw_status(f, app, rows[2]);
}

/// The MAIN pane's INNER rect (inside its border) for the given full terminal
/// area — this is exactly what the watch pty is sized to.
pub fn watch_area(area: Rect) -> Rect {
    let body = body_rect(area);
    let main_col = body_columns(body)[1];
    // Inside the MAIN block's border (1 cell each side).
    Rect {
        x: main_col.x + 1,
        y: main_col.y + 1,
        width: main_col.width.saturating_sub(2),
        height: main_col.height.saturating_sub(2),
    }
}

fn draw_title(f: &mut Frame, app: &App, area: Rect) {
    let watching = if app.main_is_watch() {
        " · WATCHING (Ctrl+x c to detach)"
    } else {
        ""
    };
    let title = format!(" journey-tui — subagent cascade{watching} ");
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            title,
            Style::default()
                .fg(Color::Black)
                .bg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ))),
        area,
    );
}

fn draw_body(f: &mut Frame, app: &mut App, area: Rect) {
    let [rail_col, main_col] = body_columns(area);

    let rail = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(45), Constraint::Percentage(55)])
        .split(rail_col);

    draw_folder_panel(f, app, rail[0]);
    draw_subagent_panel(f, app, rail[1]);
    draw_main(f, app, main_col);
}

fn panel_border(title: &str, focused: bool) -> Block<'_> {
    let style = if focused {
        Style::default().fg(Color::Cyan)
    } else {
        Style::default().fg(Color::DarkGray)
    };
    Block::default()
        .borders(Borders::ALL)
        .border_style(style)
        .title(Span::styled(
            format!(" {title} "),
            if focused {
                Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(Color::Gray)
            },
        ))
}

fn draw_folder_panel(f: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Focus::Folder;
    let block = panel_border("FOLDER STRUCTURE", focused);
    let inner = block.inner(area);
    f.render_widget(block, area);

    app.folder_hit.clear();

    let visible = inner.height as usize;
    let n = app.folder.visible_len();
    let sel = app.folder_selected.min(n.saturating_sub(1));
    let start = if sel >= visible { sel + 1 - visible } else { 0 };

    let mut lines: Vec<Line> = Vec::new();
    for i in start..n.min(start + visible) {
        let Some(node) = app.folder.node_at(i) else {
            continue;
        };
        let y = inner.y + (i - start) as u16;
        app.folder_hit.push((y, i));

        let depth = app.folder.depth_at(i).saturating_sub(1);
        let indent = "  ".repeat(depth as usize);
        let glyph = if node.is_dir {
            if node.expanded {
                "▾ "
            } else {
                "▸ "
            }
        } else {
            "  "
        };
        let name_color = if node.is_dir {
            Color::Cyan
        } else if node.is_previewable() {
            Color::White
        } else {
            Color::DarkGray
        };
        let is_sel = focused && i == sel;
        let base = if is_sel {
            Style::default().bg(Color::Rgb(40, 40, 60)).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{indent}{glyph}"), base.fg(Color::DarkGray)),
            Span::styled(node.name.clone(), base.fg(name_color)),
        ]));
    }

    f.render_widget(Paragraph::new(lines), inner);
}

fn draw_subagent_panel(f: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Focus::Subagent;
    let title = format!("SUBAGENT STRUCTURE ({} journeys)", app.cascade.journeys.len());
    let block = panel_border(&title, focused);
    let inner = block.inner(area);
    f.render_widget(block, area);

    app.cascade_hit.clear();
    // Record the panel geometry for wheel-scroll routing + scroll clamping.
    app.cascade_rect = (inner.x, inner.y, inner.width, inner.height);
    let visible = inner.height as usize;
    app.cascade_view_h = visible;

    if app.cascade_rows.is_empty() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "loading cascade…",
                Style::default().fg(Color::DarkGray),
            ))),
            inner,
        );
        return;
    }

    // Independent scroll: `cascade_scroll` is the first visible row. Clamp it to
    // the valid range in case the row count shrank since the last input.
    let n = app.cascade_rows.len();
    let max_start = n.saturating_sub(visible);
    if app.cascade_scroll > max_start {
        app.cascade_scroll = max_start;
    }
    let start = app.cascade_scroll;

    let mut lines: Vec<Line> = Vec::new();
    for (i, row) in app.cascade_rows.iter().enumerate().skip(start).take(visible) {
        let y = inner.y + (i - start) as u16;
        app.cascade_hit.push((y, i));

        let is_sel = focused && app.cascade_selected == Some(i);
        lines.push(render_cascade_row(row, is_sel));
    }

    f.render_widget(Paragraph::new(lines), inner);

    // Overflow indicator (top-right): ▲ when rows exist above the viewport, ▼
    // when rows exist below — so a knight below the fold (e.g. a freshly-added
    // coder at the bottom) is discoverable.
    if n > visible && inner.width > 3 {
        let up = if start > 0 { "▲" } else { " " };
        let down = if start < max_start { "▼" } else { " " };
        let ind_area = Rect {
            x: inner.x + inner.width - 2,
            y: inner.y,
            width: 2,
            height: 1,
        };
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("{up}{down}"),
                Style::default().fg(Color::Yellow),
            ))),
            ind_area,
        );
    }
}

fn render_cascade_row(row: &TreeRow, is_sel: bool) -> Line<'static> {
    if row.is_header {
        return Line::from(Span::styled(
            row.label.clone(),
            Style::default()
                .fg(Color::Magenta)
                .add_modifier(Modifier::BOLD),
        ));
    }

    // Indent by depth; depth 1 = direct child of a header (the King).
    let indent_levels = row.depth.saturating_sub(1) as usize;
    let branch = if indent_levels == 0 {
        String::new()
    } else {
        let mut s = "  ".repeat(indent_levels.saturating_sub(1));
        s.push_str(if row.is_last { "└ " } else { "├ " });
        s
    };

    let (glyph, gcolor) = match row.state {
        LiveState::Alive => ("●", Color::Green),
        LiveState::Offline => ("○", Color::DarkGray),
        LiveState::NotFound => ("◌", Color::DarkGray),
    };
    let dim = matches!(row.state, LiveState::Offline | LiveState::NotFound);

    let ctx = row
        .context_pct
        .map(|p| format!("{p:>4.1}%"))
        .unwrap_or_else(|| "  --".to_string());

    let base = if is_sel {
        Style::default().bg(Color::Rgb(40, 40, 60)).add_modifier(Modifier::BOLD)
    } else {
        Style::default()
    };
    let label_color = if dim { Color::DarkGray } else { Color::White };

    Line::from(vec![
        Span::styled(branch, base.fg(Color::DarkGray)),
        Span::styled(format!("{glyph} "), Style::default().fg(gcolor)),
        Span::styled(truncate(&row.label, 18), base.fg(label_color)),
        Span::styled(format!(" {ctx}"), base.fg(Color::Yellow)),
    ])
}

fn draw_main(f: &mut Frame, app: &mut App, area: Rect) {
    // Watch/Edit get a focus-aware colored border; other views a plain border.
    let is_pty = app.main_is_watch();
    let focused = app.focus == Focus::Main;
    let block = if is_pty {
        let color = if focused { Color::Green } else { Color::DarkGray };
        Block::default()
            .borders(Borders::ALL)
            .border_style(Style::default().fg(color))
            .title(Span::styled(
                pty_title(app),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ))
    } else {
        Block::default().borders(Borders::ALL).title(" MAIN ")
    };
    let inner = block.inner(area);
    f.render_widget(block, area);

    // Record the MAIN inner rect for click-to-focus routing.
    app.main_rect = (inner.x, inner.y, inner.width, inner.height);

    // Apply the active pty's scrollback offset so the drawn screen reflects the
    // history view (re-applied each frame → at offset 0 we auto-follow live).
    if is_pty {
        app.active_pty_apply_scrollback();
    }

    match &app.main {
        MainView::Empty => {
            app.main_view_h = inner.height;
            app.main_total_lines = 1;
            f.render_widget(
                Paragraph::new(Line::from(Span::styled(
                    "Select a knight (SUBAGENT) or a file (FOLDER). w=watch/revive · e=edit file.",
                    Style::default().fg(Color::DarkGray),
                ))),
                inner,
            );
        }
        MainView::Knight => {
            let body = knight_detail_lines(app);
            render_scrollable(f, app, inner, &[], body);
        }
        MainView::Preview { path, content } => {
            // Pinned 2-line header (title + divider); the body scrolls.
            let header = vec![
                Line::from(Span::styled(
                    format!("preview: {path}"),
                    Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
                )),
                Line::from(Span::styled(
                    "─".repeat((inner.width as usize).min(120)),
                    Style::default().fg(Color::DarkGray),
                )),
            ];
            let body: Vec<Line> = content
                .lines()
                .map(|l| {
                    Line::from(Span::styled(l.to_string(), Style::default().fg(Color::Gray)))
                })
                .collect();
            render_scrollable(f, app, inner, &header, body);
        }
        MainView::Watch(w) => draw_pty_pane(f, &w.parser(), inner, focused),
        MainView::Edit(e) => draw_pty_pane(f, &e.parser(), inner, focused),
        MainView::Reviving { label, text } => {
            let body = vec![
                Line::from(Span::styled(
                    format!("reviving {label}"),
                    Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
                )),
                Line::from(""),
                Line::from(Span::styled(text.clone(), Style::default().fg(Color::Gray))),
                Line::from(""),
                Line::from(Span::styled(
                    "resuming the saved session (context intact) — this is not a fresh spawn.",
                    Style::default().fg(Color::DarkGray),
                )),
            ];
            render_scrollable(f, app, inner, &[], body);
        }
        MainView::Transcript { title, content } => {
            // Pinned 2-line header (title + divider); the transcript body
            // scrolls (PageUp/Down/Home/End/wheel via the shared main_scroll).
            let header = vec![
                Line::from(Span::styled(
                    format!("TRANSCRIPT: {title}"),
                    Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
                )),
                Line::from(Span::styled(
                    "─".repeat((inner.width as usize).min(120)),
                    Style::default().fg(Color::DarkGray),
                )),
            ];
            let body: Vec<Line> = content
                .lines()
                .map(|l| {
                    // Role headers ("── ROLE ──") get a brighter style so the
                    // turn boundaries stand out from the body text.
                    let style = if l.starts_with("── ") {
                        Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)
                    } else {
                        Style::default().fg(Color::Gray)
                    };
                    Line::from(Span::styled(l.to_string(), style))
                })
                .collect();
            render_scrollable(f, app, inner, &header, body);
        }
    }
}

/// Render a scrollable read-only content view: optional pinned `header` lines
/// (drawn unscrolled at the top) plus a `body` that scrolls by
/// `app.main_scroll`. Records the body viewport height + total for clamping,
/// and draws a `▲/▼ more` indicator when content overflows.
fn render_scrollable(
    f: &mut Frame,
    app: &mut App,
    inner: Rect,
    header: &[Line<'static>],
    body: Vec<Line<'static>>,
) {
    let hdr_h = header.len() as u16;
    // Body region is inner minus the pinned header.
    let body_area = Rect {
        x: inner.x,
        y: inner.y + hdr_h,
        width: inner.width,
        height: inner.height.saturating_sub(hdr_h),
    };

    // Record for the scroll clamp (see App::scroll_main_*).
    app.main_view_h = body_area.height;
    app.main_total_lines = body.len() as u16;

    // The scroll ceiling is only knowable here (post-measure). Pin the offset to
    // the real max when either the view is "stuck to bottom" (e.g. a transcript
    // just opened at its newest turn) OR the live offset overflowed a shrunken
    // body. Both cases resolve to "clamp down to max".
    let max = app.main_total_lines.saturating_sub(app.main_view_h);
    if app.main_stick_bottom || app.main_scroll > max {
        app.main_scroll = max;
    }

    // Draw pinned header (if any) unscrolled.
    if hdr_h > 0 {
        let hdr_area = Rect {
            height: hdr_h,
            ..inner
        };
        f.render_widget(Paragraph::new(header.to_vec()), hdr_area);
    }

    // Draw the scrolled body.
    f.render_widget(
        Paragraph::new(body).scroll((app.main_scroll, 0)),
        body_area,
    );

    // Overflow indicator (top-right of the body): ▲ when scrolled, ▼ when more
    // below. Cheap, optional nicety.
    if app.main_total_lines > app.main_view_h && body_area.width > 3 {
        let up = if app.main_scroll > 0 { "▲" } else { " " };
        let down = if app.main_scroll < max { "▼" } else { " " };
        let ind = format!("{up}{down}");
        let ind_area = Rect {
            x: body_area.x + body_area.width - 2,
            y: body_area.y,
            width: 2,
            height: 1,
        };
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                ind,
                Style::default().fg(Color::DarkGray),
            ))),
            ind_area,
        );
    }
}

fn pty_title(app: &App) -> String {
    let sb = app.active_pty_scrollback();
    let sb_txt = if sb > 0 {
        format!(" ▲SCROLLBACK -{sb}")
    } else if app.active_pty_on_alt_screen() {
        // Full-screen app: no overlay scrollback; tell the user honestly.
        " [full-screen app — scroll inside it]".to_string()
    } else {
        String::new()
    };
    match &app.main {
        MainView::Watch(w) => {
            let mode = if w.read_only { "read-only" } else { "interactive" };
            let focushint = if app.focus == Focus::Main {
                "focused"
            } else {
                "Tab/click to focus"
            };
            format!(" WATCH {} [{mode}] ({focushint}){sb_txt} ", w.tmux_session)
        }
        MainView::Edit(e) => {
            let name = e
                .file
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|| "file".to_string());
            let focushint = if app.focus == Focus::Main {
                "focused"
            } else {
                "Tab/click to focus"
            };
            format!(" EDIT {name} [{}] ({focushint}){sb_txt} ", e.editor)
        }
        _ => " MAIN ".to_string(),
    }
}

fn knight_detail_lines(app: &App) -> Vec<Line<'static>> {
    let mut lines: Vec<Line> = Vec::new();
    match app.selected_row() {
        Some(row) => {
            lines.push(Line::from(Span::styled(
                "selected knight",
                Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
            )));
            lines.push(Line::from(""));
            lines.push(kv("role/agent", &row.label));
            lines.push(kv(
                "session",
                row.session_id.as_deref().unwrap_or("--"),
            ));
            lines.push(kv(
                "state",
                match row.state {
                    LiveState::Alive => "alive",
                    LiveState::Offline => "offline",
                    LiveState::NotFound => "offline (no live runtime)",
                },
            ));
            lines.push(kv(
                "context",
                &row.context_pct
                    .map(|p| format!("{p:.1}%"))
                    .unwrap_or_else(|| "--".to_string()),
            ));
            lines.push(kv(
                "level",
                &row
                    .level
                    .map(|l| l.to_string())
                    .unwrap_or_else(|| format!("(depth {})", row.depth)),
            ));
            lines.push(kv(
                "tmux",
                row.tmux_session.as_deref().unwrap_or("<unknown>"),
            ));
            if let Some(p) = &row.purpose {
                lines.push(kv("purpose", p));
            }
            lines.push(Line::from(""));
            let hint = match (&row.tmux_session, row.state) {
                (Some(_), LiveState::Alive) => "press w → watch this knight live in MAIN",
                (_, LiveState::Offline) | (_, LiveState::NotFound) => {
                    "press w → REVIVE (resume saved session) and watch in MAIN"
                }
                (None, _) => "(no tmux session recorded — a stable name will be derived on revive)",
            };
            lines.push(Line::from(Span::styled(
                hint,
                Style::default().fg(Color::DarkGray),
            )));
        }
        None => {
            lines.push(Line::from(Span::styled(
                "No knight selected.",
                Style::default().fg(Color::DarkGray),
            )));
        }
    }
    lines
}

fn draw_status(f: &mut Frame, app: &App, area: Rect) {
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            format!(" {}", app.status),
            Style::default().fg(Color::Black).bg(Color::Cyan),
        ))),
        area,
    );
}

fn kv(key: &str, value: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{key:>12}: "), Style::default().fg(Color::DarkGray)),
        Span::styled(value.to_string(), Style::default().fg(Color::White)),
    ])
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        format!("{s:<max$}")
    } else {
        let mut out: String = s.chars().take(max.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}

/// Render a live pty pane (Watch OR Edit — both feed a `vt100::Parser`) into
/// `inner`. Places the terminal cursor only when this pane is focused.
fn draw_pty_pane(
    f: &mut Frame,
    parser: &vt100::Parser,
    inner: Rect,
    focused: bool,
) {
    let screen = parser.screen();
    let (srows, scols) = screen.size();
    let rows_to_draw = srows.min(inner.height);
    let cols_to_draw = scols.min(inner.width);

    let mut lines: Vec<Line> = Vec::with_capacity(rows_to_draw as usize);
    for row in 0..rows_to_draw {
        let mut spans: Vec<Span> = Vec::new();
        let mut col = 0u16;
        while col < cols_to_draw {
            match screen.cell(row, col) {
                Some(cell) if cell.has_contents() => {
                    let mut style = Style::default()
                        .fg(vt_color(cell.fgcolor(), true))
                        .bg(vt_color(cell.bgcolor(), false));
                    if cell.bold() {
                        style = style.add_modifier(Modifier::BOLD);
                    }
                    if cell.italic() {
                        style = style.add_modifier(Modifier::ITALIC);
                    }
                    if cell.underline() {
                        style = style.add_modifier(Modifier::UNDERLINED);
                    }
                    if cell.inverse() {
                        style = style.add_modifier(Modifier::REVERSED);
                    }
                    spans.push(Span::styled(cell.contents().to_string(), style));
                    col += if cell.is_wide() { 2 } else { 1 };
                }
                _ => {
                    spans.push(Span::raw(" "));
                    col += 1;
                }
            }
        }
        lines.push(Line::from(spans));
    }

    f.render_widget(Paragraph::new(lines), inner);

    // Place the terminal cursor only when the watch pane is focused (so input
    // visibly lands where the user is typing).
    if focused && !screen.hide_cursor() {
        let (cr, cc) = screen.cursor_position();
        if cr < inner.height && cc < inner.width {
            f.set_cursor_position((inner.x + cc, inner.y + cr));
        }
    }
}

/// Convert a vt100 color to a ratatui color. `is_fg` picks a sensible default
/// for terminal-default cells.
fn vt_color(c: vt100::Color, is_fg: bool) -> Color {
    match c {
        vt100::Color::Default => {
            if is_fg {
                Color::Gray
            } else {
                Color::Reset
            }
        }
        vt100::Color::Idx(i) => Color::Indexed(i),
        vt100::Color::Rgb(r, g, b) => Color::Rgb(r, g, b),
    }
}
