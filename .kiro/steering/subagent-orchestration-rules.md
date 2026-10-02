---
inclusion: manual
---

# Subagent Orchestration Rules — the Realm's shared constitution

This document is the **single source of the hard-won mechanics** for driving knights
(kiro-cli sessions in tmux) through the motor. Any agent — the King, the Steward, or a
knight that spawns its own sub-knights — reads THIS to learn *how* to orchestrate. Roles
(who decides, who routes, who executes) live in the agent files; the *mechanics* live here.

Invocation on this host: **only `python3` exists (no `python` shim). Always
`python3 -m motor <tool>` from `~/tmux-mainbrain`.** There is no `python3 -m journey` CLI —
the journey layer is a Python API (`import journey`).

---

## 1. THE POST / GET DUALITY (the core discipline)

Two channels, never confused:

| Channel | Verb(s) | Purpose |
|---------|---------|---------|
| **POST** (write) | tmux: `spawn` / `resume` / `deliver` / gate-answers / `kill-session` | change a knight's state: create it, command it, answer its gates, end it. |
| **GET** (read) | `read_turns` / `read_verdict` / `search` | read a knight's CLEAN, pre-distilled content. |
| **STATE only** | `peek` | process state ONLY: IDLE / BUSY / GATE. A few lines. **NEVER content.** |

**Mantra: "tmux to POST, read_turns to GET, peek to see state."**

### WHY peek must never be a content getter (measured)
The V3 TUI **echoes the whole pasted prompt back** into the pane. So a `peek` capture scales
with the *prompt* size, not the *answer* size. Measured on a real session: a **102-byte**
answer produced a **6682-byte** peek — the actual new content was **~1.5%** of the capture
(1 useful line out of 51; the rest was echoed prompt + banner + status line). Repeatedly
peeking for content re-captures the same echoed blocks and **inflates the orchestrator's
context fast**, burying the one line that matters. On disk the ratio is just as stark:
assistant speech is ~6.4% of a turn's bytes (the rest is tool_call/tool_result/metadata).

Therefore: **for content, GET the transcript** (`read_turns` / `read_verdict`), which the motor
reconstructs clean (assistant chunks concatenated per turn; tool_call/tool_result/control
events excluded; no terminal noise; no viewport limit). Use `peek` only to answer
"is it IDLE, BUSY, or sitting on a GATE?"

### The GET verbs (motor)
- `read_turns <session_id> [--last N] [--role assistant|user|all] [--since OFFSET] [--spill PATH]`
  — clean turns. `--since` takes a byte-offset **cursor** (returned in every result) and returns
  only the delta since your last read + a new cursor: **incremental GET, no re-reading the whole
  transcript.** `--spill PATH` also writes the selected turns to a file (for ephemeral agents).
- `read_verdict <session_id> [--timeout N] [--interval F]` — poll the transcript incrementally
  until the last assistant turn carries a `FINAL VERDICT` block; return ONLY that block, intact.
  See §2.
- `search <query> [--role ...] [--limit N] [--case-sensitive]` — grep THROUGH the turns of ALL
  sessions on disk (content, not filenames). Returns `{session_id, agent, role, snippet, ts,
  turn_index}` per match. This powers the Steward's *search-old-sessions* capability.

---

## 2. CHOOSING YOUR DELEGATION PATH — native `subagent` vs the motor

Every agent can drive sub-knights **two ways**. They are not rivals; pick the one that fits the
task, and **say which and why**. When unsure, ASK the dev before committing.

**Native `subagent` tool** (`invoke_subagent`) — the official, lightweight path. The sub-agent runs
in an ISOLATED context, in parallel, auto-selected by its `description` (or named), with DAG
dependencies and review loops, and the parent waits for aggregated results. Requires `subagent` in
the commanding agent's `tools`. **Prefer it for:** one-shot, self-contained sub-tasks; fan-out
parallelism ("analyze these 3 tickets at once"); anything that does not need to outlive the turn.
Cost: the sub-agent is ephemeral — no durable resume, no cross-session reuse, no context-cost
routing.

**The motor / tmux path** (`spawn`/`resume`/`deliver`/`watch`/`read_verdict`, per this doc) — the
durable superset. **Prefer it for:** long-lived knights you will re-command across a journey;
resuming a knight after a reboot (`resume <session_id>`); reusing/searching PAST sessions
(`search`, `context_of` routing); knights the Steward composed from repo agents / cut turns; and any
recursion where a sub-knight itself must persist. Cost: heavier (a real tmux runtime, gate-draining,
the FINAL VERDICT contract).

**Rule of thumb:** *ephemeral & parallel → native `subagent`; durable, reusable, or resumable →
motor.* The commanding agent CHOOSES and EXPLAINS the choice; **when unsure, ASK the dev.**

---

## 2A. THE KEEP-vs-DISCARD THRESHOLD & THE SPAWN-DEPTH CEILING (the heart of cheap-but-smart orchestration)

The delegation *path* (§2) is *how* you spawn; this is *whether* and *how deep*. It is the Realm's
central threshold decision, made by a **self-Socratic judgment before every spawn** — never reflex.

### The two spawn modes — chosen by the NATURE of the task
- **spawn-and-KEEP** — the sub-knight builds **continuous intelligence** toward a long result: a
  coder iterating on a module, an investigator deepening a subject. Keep it **alive with its context
  loaded**; do NOT summarize-and-discard each step — that throws away the very context that makes it
  smarter and pays the **hidden tax of summary** (lost nuance + re-work). *Why summarize if you can
  keep it loaded until the final result?*
- **spawn-and-DISCARD** — the sub-knight does a **noisy, one-shot read** (scan 200 files for X). It
  carries the noise in a **child context**, returns ONLY a `FINAL VERDICT` (summary up, raw context
  down), and is dismissed. Keeping it would be pure waste.

### The spawn-depth ceiling — persistent knights form a KEEP tree, capped at 3 levels
```
L1  EPIC (King)          — governs the journey (keep)
L2  TASK (Knight)        — a durable knight per area: code / test / doc … (keep)
L3  SPECIALIST (Knight)  — coder / reviewer / builder / lab-controller … (keep)
      └─ discard-only    — an L3 leaf MAY spawn a one-shot instrument (noisy read) that
                           returns a verdict and dies (spawn-and-discard); it does NOT keep.
```
**Rule:** *keep-spawn is allowed to depth 3; a PERSISTENT (keep) knight beyond L3 requires explicit
King/dev approval.* A depth-3 leaf may still **spawn-and-DISCARD** instruments — they don't persist,
so they don't grow the keep-tree. This bounds the recursion of **live** knights while letting the
leaf offload noisy reads freely.

### The self-Socratic pre-spawn judgment (every agent, before spawning)
1. **Can I do this myself** with the tools I already have? If yes → do NOT spawn. (Spawn is for a
   capability I lack or context I must not pollute — never to avoid thinking.)
2. **KEEP or DISCARD?** Does this build continuous intelligence (KEEP) or is it a one-shot noisy
   read (DISCARD)? Choose the mode.
3. **What is my current depth?** A persistent (keep) spawn beyond L3 needs approval; a discard
   instrument at the leaf is fine.
4. **motor or native `subagent`?** (durable/resumable/long → motor; one-shot/parallel → native
   `subagent`, per §2) — choose and be ready to explain; unsure → ASK the King/dev.

### Tie to the Steward's context-cost gate (same wisdom, two angles)
Keep a knight alive only while it stays **cheap**: reuse `< 30%` context, judge `30–50%`, and beyond
`50%` **distil-and-refresh** (spawn fresh from cut turns). spawn-and-keep and the `<30 / 30–50 / >50`
gate are the **same principle** — persist what stays cheap-and-smart, retire what has grown
expensive.

> **Golden rule:** *delegate for a conclusion, not to avoid thinking; every sub-knight has a task
> that converges and dismisses it; summaries flow up, raw context stays down; a persistent tree is
> capped at depth 3.*

---

## 3. THE FINAL VERDICT CONTRACT (bilateral — sacred)

The contract that lets the orchestrator GET a knight's conclusion cheaply, without peeking.

- **The knight's obligation:** on concluding an order, a knight **ends its last answer** with a
  line containing exactly `FINAL VERDICT`, followed by a **concise, pre-processed summary** —
  only the size the answer needs, no rigid schema. Everything from that marker to end-of-turn
  is the verdict body (newlines preserved). It is the knight's job to distil; the orchestrator
  must not have to.
- **The orchestrator's obligation:** to collect a knight's result, **GET it via `read_verdict`**
  (never `peek`, never a full `read_turns` dump). `read_verdict` returns only the block.
- **The marker:** the literal string `FINAL VERDICT`. If it repeats across turns, the LAST one
  in the latest assistant turn wins.
- **Timeout → nudge:** if `read_verdict` times out it reports either `state: in-progress`
  (the transcript is still growing — the knight is working; wait or extend) or `state: no-marker`
  (a turn finished with no marker) plus the last-turn `tail` so you can decide. When it finished
  without the marker, POST this exact nudge into the knight:
  **`ALWAYS INCLUDE "FINAL VERDICT" IN LAST ANSWER SCOPE`**
  then GET again.
- **Recursion:** a knight that spawns sub-knights applies the SAME contract to them — it GETs
  their verdicts with `read_verdict` and only rolls the distilled result into its OWN verdict.

---

## 4. THE ATOMIC COMMAND-A-KNIGHT RITUAL (do this every time)

1. **`peek`** the target pane — confirm it is at a ready prompt (not mid-task, not on a gate).
2. **`deliver --prompt-file <path>`** (atomic paste) OR a single-line `--prompt`. NEVER raw
   multi-line `send-keys` (see the NEWLINE TRAP).
3. **`peek`** right after delivering — confirm the payload landed as ONE block (no
   "◇ N messages queued" banner).
4. **Watch with SHORT, bounded probes** — not one long generous timeout. A frozen gate never
   renders BUSY and would burn the whole timeout. Prefer a `peek`-loop: `peek` → if GATE, answer
   it; if BUSY, wait 8–12s; if IDLE, done. `watch` (30–60s) is fine for a genuine BUSY→IDLE.
5. **GET the result** via `read_verdict` (a concluding order) or `read_turns` (mid-work content).
   Reference it; never copy a whole transcript into your own context.

### The NEWLINE TRAP — the single most important POST rule
`tmux send-keys` treats a newline as **Enter**: a naive multi-line prompt **submits at the first
newline**, the knight receives only line 1, and the rest pile up as "◇ N messages queued".
Defense: the motor's `deliver` loads the payload into a tmux buffer and pastes it with `-p`
(bracketed-paste markers ESC[200~ … ESC[201~) so the V3 TUI treats it as ONE multi-line input,
then sends a single Enter. **PREFER `deliver --prompt-file` for anything multi-line or long.**
⚠ `delivered: true` does NOT prove the knight received it whole — after delivering, `peek` and
confirm there is no "messages queued" banner. Verify the delivery; never trust the promise.

### The `--no-require-ready` fallback (motor ready-check false-negatives)
`deliver` asserts the pane is at a ready input state before pasting. Its readiness heuristic keys
off the **visible viewport** (last ~200 lines); on a freshly-spawned session the idle marker can
sit in scrollback while the viewport is momentarily blank, so `deliver` may refuse with
"pane is not at a ready input state" even though a `peek` of the scrollback shows the idle prompt.
When you have **peeked and confirmed the knight is genuinely ready** but `deliver` still refuses,
either give it a few more seconds and retry, or POST with **`--no-require-ready`** (then you MUST
`watch`-for-busy afterward to confirm acceptance). Do not blind-fire `--no-require-ready` without a
prior state peek.

---

## 5. THE APPROVAL-GATE REALITY (nested kiro-cli)

A spawned knight is a full `kiro-cli` chat; when it runs a tool it may pause on an approval menu
and stay BLOCKED until answered. `watch` will see it as busy forever.

- **Prevent at the source (preferred, incomplete):** the knight's agent file should grant
  `permissions: rules: [{capability: all, effect: allow}]`. This covers the motor-driving Trinity.
  ⚠ HARD-WON: it is NOT a complete cure — spawned worker knights can still hit a gate on
  `fs_read`/`fs_write`, especially for paths OUTSIDE the workspace. Expect gates regardless.
- **Answer the gate through tmux — the TWO-LEVEL trust menu.** The V3 approval UI is a menu; the
  durable "always allow" is a **second level**: selecting *Trust* opens a sub-menu whose correct
  choice is **"Trust entire tool"** (persists for the whole tool, not just this one path/call).
  Navigate with arrow-key NAMES then Enter — `tmux send-keys -t <session> Down Enter` (the names
  `Down`/`Enter`, not literal text). Cursor position is not guaranteed — `peek` BEFORE and AFTER
  to confirm what is selected and that it advanced. A per-call "Allow" clears THIS gate only;
  choosing **Trust entire tool** stops the same tool from gating again.
- **The drain-gates loop (practical pattern):** a knight doing real work opens many files/MCP
  calls, each its own gate. Do NOT sit on a long `watch`. Loop: `peek` → if GATE, send the
  trust keys (and prefer *Trust entire tool*); if BUSY, wait a few seconds; if IDLE, done. Keep
  each wait short (8–12s) and bounded.
- ⚠ **Do NOT send keys blindly.** Only send `Down Enter` when a real approval MENU is rendered.
  An SSH **password prompt** or a knight's inner-tmux is NOT a gate — firing keys there corrupts
  or KILLS the pane. Let lab-controllers drive their own SSH; peek gently; answer only genuine
  menus. Firing many rapid `C-c` to drain a message-queue can KILL a pane — prefer answering gates
  over spamming Ctrl-C.

### The NESTED-SHELL caveat
A knight is `kiro-cli` inside tmux, possibly itself spawned by another `kiro-cli`. Its shell tool
runs in that nested pane and can inherit a broken environment — you may see `spawn /bin/bash
ENOENT` even though `/bin/bash` exists for you. In practice this appeared only in a **hollow
default-agent** session (front matter failed to load); a *properly spawned* agent runs shell fine.
Confirm the agent loaded — the status line (e.g. `wise-king · Auto · ◔ 2%`) shows the real agent;
if it says `Default`, the agent file failed to load and you are talking to a hollow vessel:
fix and re-spawn.

### capture timing & correctness
- After `deliver`, the knight needs time to render. `watch` detects completion by a BUSY→IDLE
  **transition** (or a sentinel, or N stable idle samples) — a single snapshot lies.
- The pane is a fixed-size viewport; scrollback needs `peek --lines N`. A long answer may exceed
  the visible pane — but for CONTENT, GET the transcript, don't scroll the pane.
- tmux status lines, borders, and the kiro banner are visual noise in a capture. Read past them.

---

## 6. RECOVERY (the fluid model)

The tmux runtime is ephemeral; the kiro context is durable in the `session_id`. If a knight's
tmux is gone (reboot/crash) but its `session_id` survives, rebuild the runtime with
`python3 -m motor resume <session_id> --tmux <name>` and update the pointer. Save every knight's
`session_id` (and your own) BEFORE resting — a paused journey with unsaved ids is unrecoverable
after a reboot. A resume that hangs on "Initializing" usually means a **stale `.lock`** in the
session dir left by a killed process; if its PID is dead, remove the lock and resume clean.

---

## 7. STEWARD CAPABILITIES (the four powers — mechanics)

The Steward's doctrine (routing/reuse decisions) lives in `palace-steward.md`; the mechanics are
here so any orchestrator can perform them.

1. **Start a knight from ANY repo.** `python3 -m motor spawn <agent> --cwd <repo>` — the knight is
   born inside that repo's context (its `.kiro/`, its code), while the Realm's stable ground stays
   `~/tmux-mainbrain`. Use this to give a knight the working directory it actually needs.
2. **Search context through old sessions.** `python3 -m motor search "<query>"` greps the turns of
   ALL past sessions. Use it to find a prior knight/turn relevant to a new task — feeding a
   reuse-vs-spawn decision — instead of spawning blind.
3. **Create ephemeral agents from cut turns.** Extract the relevant turns of an old session with
   `read_turns <sid> --spill <path>`, distil them into a NEW agent file, and place it where V3 will
   discover it (see §7). This is **birth-by-injection as a FILE**, not a transient prompt: the
   ephemeral agent is traceable to the journey, reactivatable, and composable from a prior
   ephemeral. Name them `ephemeral-<name>.md`.
4. **Knights can spawn sub-knights.** The Steward may equip a knight with THIS document so the
   knight orchestrates its own sub-knights: it runs `python3 -m motor spawn/deliver/read_verdict`
   itself and applies the FINAL VERDICT contract recursively, GETting each sub-knight's verdict and
   folding only the distilled result into its own.

---

## 8. AGENT DISCOVERY CONSTRAINT (where an ephemeral agent must live to load)

⚠ HARD-WON (verified on kiro-cli v3, 2.20.2): the V3 loader discovers **workspace agents only from
`<cwd>/.kiro/agents/`** (plus the global `~/.kiro/agents/`). It does NOT recurse into arbitrary
`artifacts/.kiro/agents/` subtrees. So an ephemeral agent file is only spawnable **as a named
`--agent`** if it sits in a `.kiro/agents/` dir at the spawn's `--cwd` root.

Two consistent patterns:
- **Journey-scoped, but loadable:** author the ephemeral at
  `journeys/journey-<id>/artifacts/.kiro/agents/ephemeral-<name>.md` for provenance, and spawn it
  with `--cwd journeys/journey-<id>/artifacts` (so its `.kiro/agents/` is at the cwd root and V3
  finds it). This keeps the file traceable to the journey AND loadable by name.
- **Realm-loadable:** if a knight must be spawned from `~/tmux-mainbrain` by name, its file must be
  under `~/tmux-mainbrain/.kiro/agents/` — keep such ephemerals prefixed `ephemeral-` and clean
  them up at journey end.
- **Always available — birth-by-injection (KNOWLEDGE, not identity):** regardless of file
  location, you can spawn a base agent and `deliver` distilled turns / a knowledge brief as the
  first prompt. ⚠ VERIFIED LIMIT (2026-09-28): a mere prompt saying "you are now X" does NOT
  override a built-in/locked agent's identity — both `kiro_default` and `wise-king` correctly
  reject an identity-override message as an untrusted directive. So birth-by-injection reliably
  transfers *knowledge/context* (a document to work from), but NOT a new *persona* onto an
  identity-locked agent. To give a knight a genuine new identity, use a real agent FILE at the
  spawn cwd's `.kiro/agents/` (the loading path above) — that is the robust ephemeral mechanism.

Choose the pattern to fit the spawn cwd; state the constraint you relied on so the next
orchestrator is not surprised.
