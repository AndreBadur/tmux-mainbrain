# journey — the MVP young-recruit layer (Phase 2)

Wires the Phase-0 personalities to the APPROVED Phase-1 `motor` (24/24 tests).
Runs the minimal end-to-end journey loop: **create → spawn → command → recover**.
It consumes the motor's public Python API directly (imports, not shelling out)
and **never modifies** it.

## The meta.json contract (pointers + one-liners ONLY)

Each journey has ONE state file: `journeys/<journey_id>/meta.json`, exactly the
schema from pipeline.md Part II §6:

```json
{
  "journey_id": "journey-abc12345",
  "goal": "one-line purpose",
  "king_session": "sess_king...",
  "status": "active",
  "created_at": "2026-09-01T17:00:00Z",
  "updated_at": "2026-09-01T17:05:00Z",
  "knights": [
    { "role": "coder", "agent": "wrcp",
      "session_id": "sess_def...", "tmux_session": "knight-coder-1",
      "resolved": true }
  ]
}
```

`knights[]` entries are **pointers only**: `role`, `agent`, `session_id`
(durable kiro anchor), `tmux_session` (the runtime the King talks to), and a
`resolved` marker. Nothing else.

### Anti-verbosity guard (enforced in code — `journey/meta.py`)

`validate_document` / `validate_knight_entry` run on **every read and write** and
raise `AntiVerbosityError` when:

- a top-level field outside the allowed set appears;
- a `knights[]` entry carries any field beyond
  `{role, agent, session_id, tmux_session, resolved}` (e.g. a `transcript`,
  `decision`, `output`, `log`);
- any scalar string value contains a newline (`goal`, `tmux_session`, etc.).

So transcripts, decisions, domain knowledge, and multi-line documents **cannot**
be persisted here — they belong in `pipeline.md`, the kiro session `.jsonl`, or
the Archmaester. Writes are atomic (temp file + rename).

## API surface (`import journey`)

| Function | Role | Does |
|----------|------|------|
| `journey_create(goal, king_session=None, journey_id=None) -> str` | — | write meta.json (§6 schema); one-line `goal` enforced |
| `spawn_knight(journey_id, role, agent, port=REAL_MOTOR, cwd=None) -> str` | Steward (Mode 1) | `motor.spawn(agent)` → register → return ready `tmux_session` |
| `command_knight(journey_id, role_or_tmux, prompt, ...) -> str` | King (Mode 2) | deliver → watch → **return** a peek (never persisted) |
| `recover_knight(journey_id, role, port=REAL_MOTOR, cwd=None) -> str` | recovery | `motor.resume(session_id)` → update `tmux_session` |
| `register_knight(journey_id, role, agent, session_id, tmux_session, resolved=None) -> dict` | Steward | append a pointer-only knight; handles `session_id=None`/`resolved=False` |
| `resolve_knight(journey_id, role, port=REAL_MOTOR) -> dict` | Steward | fill an unresolved `session_id` via `scan_knights` (only if exactly one candidate) |
| `get_journey(journey_id) -> dict` | — | read + validate meta.json |

### Handling the motor's spawn contract
`motor.spawn` may return `{session_id: None, resolved: false}` (kiro slow to
write, or ambiguous). `spawn_knight`/`register_knight` store the `tmux_session`
and mark the knight **unresolved**; `resolve_knight` fills the durable id via a
fresh `scan_knights` before it is trusted. `recover_knight` refuses to run on an
unresolved knight (`UnresolvedKnightError`) — the anchor must exist first.

### Phase-2 hardening in `command_knight` (from 12-review.md)
The motor's stable-idle `watch` path can false-complete on a slow first token
when the busy marker never renders. `command_knight` captures a **pre-deliver
pane baseline**; after `watch` returns, if the pane is **unchanged** from the
baseline (no new output), it does **one bounded re-watch** before accepting
completion. Simple and bounded (no unbounded loops).

## Testability seam
All motor calls go through an injectable `MotorPort`. Production uses
`REAL_MOTOR`; unit tests inject a fake — **no real tmux/kiro** in the suite.

## Configuration
| Env var | Default |
|---------|---------|
| `TMUX_MAINBRAIN_JOURNEYS` | `<repo>/journeys` |

(reuses `motor.paths.repo_root()` / `TMUX_MAINBRAIN_ROOT`.)

## Tests

```
python3 journey/tests/test_journey.py      # bare runner, no pytest needed
python3 -m pytest journey/tests -q         # if pytest is available
```

13 isolated tests: exact-schema write, anti-verbosity (forbidden field +
multi-line value + multi-line goal), unresolved-then-resolve, ambiguous-stays-
unresolved, spawn register (resolved + unresolved), command happy-path (result
returned, NOT persisted), slow-first-token re-watch fires, no-re-watch when
output changed, recover resume+update, recover-unresolved raises.

## Module map

```
journey/
├── __init__.py          public API
├── errors.py            JourneyError hierarchy (extends motor.MotorError)
├── paths.py             journeys_root/meta_path (env-overridable)
├── meta.py              the anti-verbosity contract + atomic meta store
├── journey.py           the 5 actions + MotorPort seam + slow-token guard
├── README.md           this file
└── tests/test_journey.py  13 isolated tests (fake motor injected)
```
