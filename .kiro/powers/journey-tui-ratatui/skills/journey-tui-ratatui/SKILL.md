---
name: journey-tui-ratatui
description: >
  Build/edit the tmux-mainbrain journey-tui — a reactive Rust+Ratatui TUI that renders the
  subagent cascade tree and lets you click-to-watch a knight via an embedded interactive
  `tmux attach`. Activates on ratatui / tmux-mainbrain TUI / pty / vt100 / cascade / journey-tui work.
provenance: PROVEN — battle-validated (~12 features shipped, 51 tests green). Source of truth = the
  code in ~/tmux-mainbrain/tui/src/*.rs, read 2026-09-29. Supersedes the Archmaester R1–3 hypothesis.
---

# journey-tui — Rust/Ratatui TUI over the tmux-mainbrain motor (PROVEN skill)

A coder skims this to get oriented fast, then reads the named module. Everything below is what the
code ACTUALLY does — not aspiration. Project root: `~/tmux-mainbrain/tui/`.

## 1. WHAT IT IS
A **read-mostly, reactive** dashboard over the tmux-mainbrain motor. Left rail has two panels:
**FOLDER** (file tree of the motor root, read-only preview of `.md/.json/.yaml/.py`) and **SUBAGENT**
(the governed cascade tree). The **MAIN** pane is a state machine that shows knight detail, a file
preview, a live **interactive `tmux attach`** (click/`w` a node → watch AND type into the knight), or
an embedded **`$EDITOR`** (`e` on a file). Data refreshes on a 3s poll; the UI thread never blocks.
Not yet built: top-bar tabs and split-panes (MAIN hosts ONE view at a time today) — the pty component
is already tab/split-ready.

## 2. CRATE MATRIX (pinned in `tui/Cargo.toml` — keep exactly; PROVEN to build)
`ratatui = "0.30"` (re-exports crossterm) · `crossterm = "0.29"` · `portable-pty = "0.9"` ·
`vt100 = "0.16"` · `serde` + `serde_json` (feature `preserve_order`) + `anyhow = "1"`.
Edition **2021**, `rust-version = "1.88"` (MSRV). Host toolchain is 1.98 — fine (≥ MSRV).
`[profile.release] opt-level = 2`.

## 3. MODULE MAP (14 modules; one line each, from reading the code)
- **main.rs** — entry + event loop: terminal setup (alt-screen + **mandatory mouse capture**), the
  3s poll drain, revive-msg drain, `reap_dead_watch`, **≤30fps render throttle** (`FRAME_MIN=33ms`),
  the `Ctrl+x` **leader state machine**, key/mouse routing, and `encode_key` (crossterm→pty bytes).
- **app.rs** — all UI state (`App`), boundary-agnostic (never calls the motor; workers feed it).
  Holds `MainView` (the MAIN state machine), focus, selection, hit-test maps, scroll state, and the
  attach-vs-revive decision (`open_watch_for_selected`).
- **pty.rs** — the ONE shared `PtySession`: `portable-pty` master → background reader thread →
  `vt100::Parser` (behind `Mutex`) → rendered to ratatui spans; bidirectional input via
  `take_writer()`; `resize()`; **5000-line scrollback** that **self-clamps** to real history;
  `Drop` kills-if-alive + `wait()`-reaps (no zombies). `SCROLLBACK_LINES` has a compile-time non-zero assert.
- **watch.rs** — thin wrapper: `PtySession` running `tmux attach [-r] -t <session>`; `Drop` runs
  `tmux detach-client -s <session>` BEFORE dropping the pty (close = detach; knight survives).
- **edit.rs** — thin wrapper: `PtySession` running `$EDITOR <file>` (cwd = file's dir); `resolve_editor`
  = `$EDITOR` → `$VISUAL` → first of `nano`/`vi`/`vim` on PATH. No special teardown (drop reaps).
- **motor.rs** — the ONE process-boundary module: `python3 -m motor <verb>` → capture stdout → parse
  one JSON doc → typed `Knight`/`TurnsDelta`/`ResumeCleanResult` or typed `MotorError`
  (Spawn/Motor/Parse). Nothing else in the TUI shells to the motor.
- **cascade.rs** — parse `journeys/*/meta.json` into the governed tree (nest by `parent`, honor
  `level`, `tui` filter). Pure parsing; handles new-format (tree_root/parent/level) AND old-format
  (king_session, flat). Dangling parents hang under root (never dropped).
- **tree.rs** — flatten a `Cascade` + live index + `LiveTmux` into displayable `TreeRow`s (glyphs,
  indent, `is_selectable`); joins live state on `session_id`. (Rendering-shape helper for app/ui.)
- **runtime.rs** — `LiveTmux` (the set of alive tmux names, one `list-sessions` per poll) + the
  `resolve_tmux_name`/`derive_tmux_name` used by BOTH the glyph and the attach decision (never disagree).
- **poller.rs** — the background worker: on a 3s tick it loads the cascade (meta.json), lists live
  tmux, and runs `scan_knights`, pushing `PollMsg` to the UI. `poke()` forces an immediate cycle;
  clean shutdown via `recv_timeout` on a ctrl channel (no busy-wait).
- **revive.rs** — click-to-revive an OFFLINE knight: a bounded (20s) worker runs `resume_clean`,
  classifies the outcome (`Resumed{cleared_lock}` / `HeldByLive{pid}` / `Refused`), reports over a channel.
- **meta_writer.rs** — **pointer-ONLY** meta.json update: parse as untyped `Value`, mutate exactly
  `tmux_session` for one knight, **atomic write** (temp + fsync + rename). NEVER touches parent/level/role/tree.
- **fs_tree.rs** — the FOLDER file tree (lazy expand, `path_at`/`node_at`, `preview` with a byte cap,
  `is_previewable` = `.md/.json/.yaml/.py`), reloadable preserving expanded state.
- **ui.rs** — ratatui rendering: draws the two rails + MAIN, computes `watch_area` (the MAIN inner
  rect used to size the pty), writes the hit-test maps and `main_rect` back into `App` for mouse routing.

## 4. THE MOTOR BOUNDARY (TUI = Rust, motor = Python)
Only **`python3`** exists on host (no `python` shim). All calls run from `~/tmux-mainbrain` and print
ONE JSON doc to stdout (exit 0), or `{"error","type"}` + non-zero on failure. Verbs used:
- **`scan_knights`** → `{count, knights:[{agent, alive, context_pct, essence, format, last_seen,
  parent, purpose, salons, session_id, window}]}`. Feeds context%/purpose ONLY. NOTE: the index has
  `alive:bool` (not IDLE/BUSY/GATE) and its `parent` is **sparse** — the governed tree comes from
  meta.json, not this field.
- **`read_turns <sid> --since <cursor>`** → `{turns:[{role,text,ts}], cursor}` — incremental clean-turn
  tail (the reactive transcript feed; wired but the live-tail view is a future slice). `cursor` is a
  byte offset; pass it back next read.
- **`context_of <sid>`** → live ctx% / `liveness` string (a "fresh conversation" nuance, NOT runtime presence).
- **`resume_clean <sid> --tmux <name> [--parent P --journey J --role R]`** → the revive verb.
  **Contract:** dead/absent lock → auto-remove + resume `{resumed:true, tmux_session, removed_stale_lock}`;
  **LIVE-pid lock → REFUSE** `{resumed:false, reason:"held_by_live", pid}` (never kills a live process);
  missing/unparseable → `{resumed:false, reason:...}`. Blocking → call on a worker thread.
- **`read_session <sid>`** → full transcript (available; used as needed).

## 5. HARD-WON DESIGN RULES — DON'T REGRESS THESE (each with its one-line why)
- **Attach-vs-revive is decided by TMUX EXISTENCE, not motor liveness.** An idle knight reads
  `alive:false/"stale"` (conversation-fresh, not runtime-present); its tmux may be alive and
  attachable. `runtime::LiveTmux` is the authority. (app.rs `open_watch_for_selected`; runtime.rs.)
- **Close = DETACH, knight stays ALIVE.** tmux is client/server split — `detach-client` kills only the
  client. **NEVER `kill-session`/`kill-server`.** We run `attach` (a client), never the session root. (watch.rs Drop.)
- **MAIN is a state machine:** `Empty | Knight | Preview | Watch(pty) | Edit(pty) | Reviving`.
  Watch and Edit **share the one `PtySession`** — build the pty once, wrap it twice. (app.rs `MainView`.)
- **Leader is ALWAYS armable, even inside a focused pty.** `Ctrl+x c` closes/detaches; `Ctrl+x Ctrl+x`
  sends a **literal Ctrl+x** (0x18) to the child (nano save-quit); any other key cancels + forwards
  (no keystroke lost). Off-pty: `Ctrl+x` = classic leader (`c/w/q`). (main.rs leader state machine.)
- **Two scroll models, never confused.** Preview/Knight/Reviving scroll as content (`main_scroll`,
  clamped to rendered height). A pty pane scrolls its **vt100 scrollback** (5000 lines, self-clamps via
  `set_scrollback`→`scrollback()` readback). **Alt-screen apps (kiro-cli/vim/less) keep NO scrollback**
  → show the honest hint "scroll inside it", never a phantom counter. Any real keystroke **snaps to
  live** before reaching the child. (pty.rs; main.rs PageUp/PageDown.)
- **Parenting rule: parent = who COMMANDS (the requester), never who forged.** The TUI trusts
  meta.json `parent` verbatim and its writes are **pointer-only** (`tmux_session`); it never rewrites
  parent/level/role/tree. (cascade.rs; meta_writer.rs.)
- **Per-journey `tui:` filter.** `ENABLED` (case-insensitive) or **missing = shown**; any other value
  (`DISABLED`) hides the whole journey subtree. (cascade.rs `is_tui_enabled`.)
- **Non-blocking always.** Motor/tmux/resume run on worker threads → `PollMsg`/`ReviveMsg` channels.
  Force-reload `r` + auto-reload after edit/revive both `poller.poke()` for an immediate refresh
  instead of waiting the 3s tick. (poller.rs; main.rs.)

## 6. BUILD / RUN / TEST
- Build: `cargo build --release` (from `tui/`). Binary: `tui/target/release/journey-tui`.
- Run: alias `tmux-mainbrain` → the release binary. Motor root resolves from `$TMUX_MAINBRAIN` →
  `$HOME/tmux-mainbrain` → `.`. `TUI_READONLY_WATCH=1` forces every watch to `tmux attach -r`.
- Test: `cargo test` (**51 tests** green). Also run `cargo clippy` and fix before "done".
- **Keybinds:** `j/k`↑↓ move · `Tab` cycle focus (Folder→Subagent→Main) · `←/→` jump rail ·
  `Enter/Space` activate · `w` watch selected knight · `e` edit selected file · `r` reload ·
  `q` quit · `Ctrl+x c` close/detach MAIN pty · `Ctrl+x Ctrl+x` literal Ctrl+x to editor ·
  `PageUp/PageDown` scroll (content or pty scrollback) · `Home/End` top/bottom · mouse click selects,
  wheel scrolls.

## 7. GOTCHAS PROVEN IN BATTLE
- **Alt-screen apps keep no scrollback** — a full-screen TUI manages its own; don't fake an overlay
  counter (`on_alternate_screen()` → honest hint).
- **`delivered ≠ landed`** for anything pasted into an attached knight — the pty echoes; verify, don't
  trust the write. (General tmux-mainbrain law; matters when driving a watched knight.)
- **`$EDITOR`/`$VISUAL` unset → nano/vi/vim fallback**; none present → clean error, not a panic.
- **Render throttle ≤30fps** (`FRAME_MIN=33ms`) — a noisy child can't melt the CPU; the dirty flag
  gates redraws.
- **Reap the pty child on drop** — `Drop` kills-if-alive then `wait()`s; the reader thread joins on
  EOF. Skipping this leaks zombies (a claudetui-noted trap).
- **crossterm→pty keymap is not perfectly lossless** — `encode_key` covers the common set; exotic
  combos (Ctrl+Arrow, Alt+Backspace) are a known follow-up. Extend `encode_key`, don't rearchitect.
- **`scan_knights.parent` is sparse** — never build the tree from it; meta.json is authoritative.

## 8. TRANSCRIPT AS DURABLE HISTORY
`read_turns` (clean turns + byte cursor) is the durable, incremental source for a knight's full
conversation. If a "show transcript" MAIN view is ever built, feed it from `read_turns --since <cursor>`
(the reactive tail already modeled in `motor.rs` `TurnsDelta`) rather than scraping the live pane.
