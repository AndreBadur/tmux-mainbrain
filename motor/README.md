# motor — the deterministic engine (Phase 1)

The Steward's **hands**: pure mechanical functions, no AI/LLM/personality.
Same input → same output. Maps the 8 proven POCs (files 01–08) to 9 reusable
tools. The Steward (brain) decides the *what*; the motor provides the *how*.

## Invocation convention (the ONE the Steward uses)

```
python3 -m motor <tool> [args...]
```

Run from the repo root (`~/tmux-mainbrain`). This host has **no `python` shim,
only `python3`** — always use `python3`. Every tool prints a **single JSON
document** to stdout and exits `0` on success. On failure it prints
`{"error": "...", "type": "..."}` and exits non-zero (1 = MotorError,
2 = ValueError/OSError).

## The 9 tools

| Tool | Command | POC |
|------|---------|-----|
| `read_session` | `python3 -m motor read_session <session_id>` | 07 |
| `scan_knights` | `python3 -m motor scan_knights [--root DIR] [--index PATH]` | 03,04,06 |
| `context_of` | `python3 -m motor context_of <session_id>` | 04 |
| `spawn` | `python3 -m motor spawn <agent> [--tmux NAME] [--cwd DIR] [--ready-timeout N]` | 08 |
| `resume` | `python3 -m motor resume <kiro_session_id> [--tmux NAME] [--cwd DIR] [--ready-timeout N]` | 08 |
| `deliver` | `python3 -m motor deliver <tmux_session> --prompt TEXT \| --prompt-file PATH [--no-require-ready]` | 02,08 |
| `watch` | `python3 -m motor watch <tmux_session> [--timeout N] [--sentinel S] [--interval F] [--settle F] [--idle-stable-samples N]` | 02,08 |
| `peek` | `python3 -m motor peek <tmux_session> [--lines N]` | 02 |
| `mine` | `python3 -m motor mine <session_jsonl> --wing W --room R [--agent A] [--dry-run]` | 03 + MemPalace |

### Contracts callers must honour (review iter-1)

- **`spawn`** returns `{tmux_session, agent, session_id, resolved}`. When kiro
  hasn't written its session dir yet, or >1 new session appeared, `session_id`
  is `null` and `resolved` is `false` — the Steward MUST then resolve the id via
  `scan_knights` before trusting/persisting it. `session_id` is never `""`.
- **`deliver`** asserts the pane is at a ready/idle input state before pasting
  (default). It refuses (`DeliveryError`) if the pane is busy. Pass
  `--no-require-ready` ONLY if you `watch`-for-busy immediately afterward.
- **`watch`** completion is a *transition*, not a snapshot: idle is accepted as
  done only after an observed BUSY→IDLE transition, OR after
  `--idle-stable-samples` consecutive idle samples with unchanged pane content
  (for instant replies that never render "Kiro is working"). A `--sentinel` is
  only honoured once the pane is not busy, so the prompt echo cannot false-fire.

## Configuration (env overrides, no hardcoded user paths)

| Env var | Default |
|---------|---------|
| `TMUX_MAINBRAIN_ROOT` | package parent (`~/tmux-mainbrain`) |
| `TMUX_MAINBRAIN_PALACE` | `<root>/palace` |
| `KIRO_SESSIONS_ROOT` | `~/.kiro/sessions` |
| `TMUX_MAINBRAIN_FRESH_SECONDS` | `900` (fresh-lock threshold) |

## Design rules honored

- **Deterministic only** — no LLM, no prompt, no personality.
- **Null-tolerant** — fresh/pre-first-turn sessions report `not-ready`, never crash.
- **Fresh-lock rule** — `alive` = present AND touched within the threshold.
- **Fail loud** — an unrecognized session layout raises `UnknownSessionFormatError`.
- **Never mine raw** — `mine` preprocesses the `.jsonl` into clean USER/AGENT
  turns first, then invokes `mempalace mine --mode convos`.
- **Bounded** — every subprocess/tmux/poll has an explicit timeout; nothing hangs.

## Tests

```
python3 motor/tests/test_motor.py         # bare runner, no pytest needed
python3 -m pytest motor/tests -q           # if pytest is available
```

24 isolated tests (no tmux/kiro/mempalace) cover v1/v2 + v3 normalization,
null-tolerance, stale-lock death, **lockless-but-active liveness**, lineage,
fail-loud, **whole-index scan with skip-classification + systematic-format
fail-loud**, mining noise-stripping + subprocess assembly, and the
**`watch` transition / no-false-completion / sentinel-echo / timeout** and
**`spawn` id-resolution (single / ambiguous / unresolved)** logic via injected
pane-captures and session-id listers.

## Module map

```
motor/
├── __init__.py          public API (import surface)
├── __main__.py          CLI dispatcher (the Steward's entry point)
├── errors.py            tight exception hierarchy
├── knight_model.py      the normalized Knight contract (the spine's data)
├── format_adapter.py    read_session — v1/v2 + v3 detect & normalize (POC 07)
├── paths.py             env/config path resolution
├── sessions.py          scan_knights + context_of (POC 03/04/06)
├── tmux_driver.py       spawn/resume/deliver/watch/peek (POC 02/08)
├── mining.py            mine — clean turns → mempalace (POC 03 + MemPalace)
└── tests/test_motor.py  deterministic unit tests
```
