# curation — Ritual B: end-of-journey curation of validated spoils (Phase 5)

The FINAL phase and the COUNTERPART to Ritual A. Ritual A delivered hypothesis
and **never deposited**; Ritual B collects battle feedback and **deposits the
validated saber**. A thin composition over the APPROVED layers — it consumes
their public APIs and never modifies them:

| Step | Reuses |
|------|--------|
| collect knight reports | `journey.command_knight` + `journey.get_journey` |
| deposit into the palace | `motor.mine` (the ONLY deposit mechanism in the Realm) |
| lifecycle governance | `steward.keep` / `steward.retire` / `steward.delete` |
| the hypothesis being validated | `ritual.ArchmaesterSynthesis` (Ritual A) |

The one NEW piece is **provenance-tagged curation**.

## The Ritual B flow

```
King: "Each knight, bring your spoils"
  collect_spoils  → command each knight: WORKED / FAILED  → KnightReport[]
Feedback → the Archmaester curates the BATTLE-VALIDATED saber:
  curate_saber    → validate the Archmaester's drawers (provenance MANDATORY)
  deposit_saber   → motor.mine each drawer into palace/archmaester/<wing>/<room>
King arbitrates each knight's fate:
  arbitrate_lifecycle → steward.keep / retire / delete (delete stays gated)
ritual_b = collect → curate → deposit → arbitrate  (guaranteed closing step)
```

## Provenance — the not-omniscient principle, enforced in code

Every `SaberDrawer` MUST carry an explicit boolean `validated`:
- `validated: true`  — proven in battle (a knight confirmed it worked)
- `validated: false` — hypothesis (Archmaester research, not yet tested)

`drawer_from_dict` / `curate_saber` **fail loud** (`ProvenanceError`) if the flag
is absent or non-boolean. Untagged saber can never be deposited — a future
Archmaester trusts `validated:true` more; hypotheses are starting points, not
truth. The Archmaester's generative judgment of what held vs what was stale is an
**INPUT** (the `curated_drawers` it produced) — this layer validates structure,
it does not invent the classification.

## The two moments — never confused

| Moment | Who | Action | Saber | Flag |
|--------|-----|--------|-------|------|
| Ritual A (Phase 4) | Archmaester | investigate → **deliver** | hypothesis | `deposited: false` |
| Ritual B (here) | knights + Archmaester | report → validate → **deposit** | battle-proven | `validated: true/false` |

Enforced: the provenance guard runs BEFORE any deposit, so an untagged drawer is
refused and nothing is mined (test: `test_two_moments_untagged_drawer_refused_before_deposit`).

## API

```python
import curation

reports = curation.collect_spoils(journey_id, port=REAL_CURATION_PORT)
# -> [KnightReport{role, session_id, worked, failed}]  (RETURNED, not persisted)

drawers = curation.curate_saber(subject, synthesis, reports, curated_drawers, port=...)
# curated_drawers: [{wing, room, content, validated: bool, provenance_note}]
# -> [SaberDrawer]  (ProvenanceError if any drawer lacks an explicit `validated`)

result = curation.deposit_saber(drawers, port=..., dry_run=False)
# real: motor.mine per drawer into wing/room with provenance; dry_run: plan only

lifecycle = curation.arbitrate_lifecycle(journey_id, decisions, port=...)
# decisions: [{session_id, fate: keep|retire|delete, force?, confirm?}]
# delete stays GATED — confirm/force passed through, never bypassed

summary = curation.ritual_b(journey_id, subject, synthesis, curated_drawers,
                            lifecycle_decisions, port=..., dry_run=False)
# collect → curate → deposit → arbitrate; empty curated_drawers deposits nothing
# but still collects + arbitrates (the closing step is guaranteed).
```

## Design discipline
- Composition over existing layers (no re-implementation of mine/lifecycle).
- Provenance mandatory on every drawer (fail loud) — the not-omniscient rule.
- Anti-verbosity: reports/synthesis are RETURNED, never persisted to meta.json.
- Deterministic orchestration: the validated/hypothesis judgment is an INPUT.
- `delete` stays gated (confirm/force honored); tight exceptions; bounded; no
  hardcoded paths.

## Tests

```
python3 curation/tests/test_curation.py     # bare runner
python3 -m pytest curation/tests -q         # if pytest available
```

18 isolated tests (fakes for motor.mine + journey + steward; no real tmux/kiro):
collect_spoils (commands each knight, reports not persisted), curate provenance
(accept well-formed, refuse missing/non-bool flag, require content), deposit
(dry-run plans without mining, real calls mine per drawer with right wing/room,
empty no-op), arbitrate (routes keep/retire/delete, delete-without-confirm
refused, invalid fate), ritual_b (end-to-end, trivial-journey deposits nothing,
dry-run), and the two-moments guard.

## Module map

```
curation/
├── __init__.py     public API
├── errors.py       CurationError / ProvenanceError / DepositError
├── models.py       KnightReport, SaberDrawer (provenance guard), *Result
├── curation.py     collect_spoils / curate_saber / deposit_saber /
│                   arbitrate_lifecycle / ritual_b + CurationPort seam
├── README.md      this file
└── tests/test_curation.py   18 isolated tests
```
