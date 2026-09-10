# steward — the Intelligent Steward routing scaffolding (Phase 3)

The Realm's unique value: routing by **essence, not location**. Nothing else
reuses sessions intelligently. Built on the APPROVED motor (Phase 1, 24/24) and
the journey layer (Phase 2, 13/13) — it **consumes** their public APIs and never
modifies them.

## The deterministic / generative split (the critical constraint)

The Steward is a generative BRAIN; the motor is deterministic HANDS. Phase 3
routing has both — and this package holds ONLY the deterministic half:

| Concern | Who | Where |
|---------|-----|-------|
| Interpret a task → `task_salons` | Steward AGENT (LLM) | **input** to `route()` |
| Score a candidate's `match_score`, `centrality` (0..1) | Steward AGENT (semantic, MemPalace) | **input** to `route()` |
| Apply recency filter, compute coverage + capacity, pick the action | **this code** | `route()` (pure) |
| Assemble a bounded injection payload | **this code** | `inject_knowledge` |
| Execute the action via motor/journey | **this code** | `execute_decision` |

There is **no LLM and no invented semantic scoring** here. `match_score` and
`centrality` arrive as floats the Steward supplies. `route()` is a pure
function: same inputs → identical `RoutingDecision`.

## The decision tree

```
apply recency_filter (drop stale unless override)
score every candidate on the FOUR criteria:
    coverage    = (task_salons covered by candidate.salons) / (task_salons)
    match%      = candidate.match_score        (Steward-supplied)
    centrality  = candidate.centrality         (Steward-supplied)
    capacity    = headroom = 100 - context_pct
rank best-first (strong, then coverage, match, centrality, headroom)

best is "strong" iff match_score >= STRONG_MATCH_MIN
                 AND centrality  >= CENTRALITY_MIN
                 AND coverage    >= COVERAGE_MIN

no candidates / best is weak       → SPAWN         (fresh young recruit)
strong + context_pct >= NEAR_FULL  → EXTRACT_FRESH  (spawn + inject a summary)
strong + not alive                 → RESUME         (rebuild runtime, then reuse)
strong + alive + headroom < MIN    → EXTRACT_FRESH  (too little window to add work)
strong + alive + headroom OK       → REUSE          (deliver into the living knight)
```

Every `RoutingDecision.reason` names the criteria that drove it, so the Steward
can be transparent to the dev.

## Named thresholds (all env-overridable — no magic numbers)

| Constant | Env var | Default | Meaning |
|----------|---------|---------|---------|
| strong_match_min | `STEWARD_STRONG_MATCH_MIN` | 0.70 | min `match_score` for a strong match |
| centrality_min | `STEWARD_CENTRALITY_MIN` | 0.50 | min `centrality` (main subject, not tangential) |
| coverage_min | `STEWARD_COVERAGE_MIN` | 0.50 | min salon-coverage fraction |
| near_full_pct | `STEWARD_NEAR_FULL_PCT` | 80.0 | context% at/above which a knight is "near full" |
| headroom_min_pct | `STEWARD_HEADROOM_MIN_PCT` | 20.0 | min free headroom to reuse without extracting |
| max_injection_bytes | `STEWARD_MAX_INJECTION_BYTES` | 262144 | hard cap on an injection payload |
| default_recency_days | `STEWARD_DEFAULT_RECENCY_DAYS` | 60 | default recency window (~2 months) |

`capacity_policy={near_full_pct, headroom_min_pct}` can override per-call.

## API

```python
import steward

decision = steward.route(task_salons, candidates,
                         capacity_policy=None, recency_filter=None)
# candidates: [{session_id, agent, essence, salons, context_pct, window,
#               alive, last_seen, match_score, centrality}]
# recency_filter: {days: int, override: bool}
# -> RoutingDecision {action, chosen_session_id, reason, scored_candidates}

result = steward.execute_decision(journey_id, role, decision, agent,
                                  port=REAL_STEWARD_PORT, cwd=None,
                                  extract_summary=None,
                                  inject_sources=None, inject_dose="lean")
# REUSE → live tmux (resume-rebuilds a runtime if needed);
# RESUME → motor.resume; EXTRACT_FRESH → spawn+inject summary; SPAWN → fresh recruit.
# Registers the knight in meta.json as POINTERS ONLY (anti-verbosity respected).

steward.inject_knowledge(tmux_session, sources, dose="lean", port=...)
# sources: file/dir paths AND/OR {"label","text"} MemPalace cuttings.
# lean (default) = essential cuttings / top-level dir; deep = whole salon (recurse).
# Bounded payload; FAILS LOUD on a missing source path or over the byte cap.

steward.keep(session_id)
steward.retire(session_id, index)          # marks index row; never edits essence
steward.delete(session_id, force=False, confirm=True, ...)   # DESTRUCTIVE
```

### Destructive delete — heavily gated + path-safe
`delete` refuses unless `confirm=True` AND (the session is not alive OR
`force=True`). Path-traversal defense-in-depth: the `session_id` is rejected if
it contains a path separator / `..` / is empty or `.` BEFORE any filesystem
access; every removal path is asserted to resolve within the sessions root; and
deletion prefers the format-adapter-resolved `source_path` over reconstructing
paths from the raw id. It fails CLOSED on an unreadable (corrupt) session
(requires `force`); only a truly-absent session is safe without force. It never
hand-edits the index; the next `scan_knights` rebuilds it whole.

### Capacity-unknown routing
When a candidate's `context_pct` is `None` (v3 pre-first-turn / unknown-window
agent), capacity is treated as UNKNOWN — NOT zero. A strong+alive candidate with
unknown capacity is REUSED with a reason that explicitly says capacity is
unknown, so the Steward decides with eyes open (it never claims a measured "0%").

## Testability seam
Motor + journey calls go through an injectable `StewardPort`; unit tests drive
fakes (no real tmux/kiro). Production uses `REAL_STEWARD_PORT`.

## Tests

```
python3 steward/tests/test_steward.py      # bare runner
python3 -m pytest steward/tests -q         # if pytest available
```

26 isolated tests: the full decision tree (REUSE/RESUME/EXTRACT_FRESH/SPAWN incl.
empty + weak), recency drop + override, purity, four-criteria ranking, injection
(bounded / missing-source fail-loud / too-large / deliver), execute_decision
routing per action, and lifecycle (keep/retire/delete gating).

Review-loop iter-1 added 14 more (→ **40 total**): capacity-unknown routing,
`execute_decision` resolved/session_id surfacing, delete path-traversal refusal
(all reviewer exploits), delete fail-closed on corrupt read, and recency
date-parsing edges.

## Module map

```
steward/
├── __init__.py     public API
├── config.py       named env-overridable thresholds
├── errors.py       StewardError hierarchy (extends motor.MotorError)
├── routing.py      route() — the pure decision core + RoutingDecision
├── inject.py       inject_knowledge / assemble_payload (§3.x)
├── execute.py      execute_decision + StewardPort seam
├── lifecycle.py    keep / retire / delete (destructive gate)
├── README.md      this file
└── tests/test_steward.py   26 isolated tests
```
