# ritual — Ritual A: the Archmaester (Phase 4)

Strengthening before battle. When the King faces an unfamiliar subject, he summons
an **Archmaester** (via the Steward) to found the **Archimedean point** — the point
of greatest available certainty — investigate the references, synthesize, and
**DELIVER** the result to the King. The Archmaester **never deposits** during
Ritual A: its synthesis is HYPOTHESIS until battle (Ritual B / Phase 5) proves it.

This is a **thin orchestration** over the APPROVED motor + journey + steward layers.
The Archmaester is JUST ANOTHER KNIGHT — routed by the Steward by essence, spawned
and commanded through the existing machinery. This package composes those calls; it
does not re-implement spawn/deliver/watch/route.

## The Ritual A flow

```
King (subject unfamiliar) → ritual_a(journey_id, subject, refs)
  a. candidates supplied?  → steward.route (reuse an archmaester who studied X)
     else                  → spawn a fresh archmaester knight
  b. register/spawn the archmaester as a POINTER-ONLY knight in the journey
  c. command it with build_investigation_prompt(subject, refs)
  d. command_knight delivers → watches → peeks the synthesis
  e. return ArchmaesterSynthesis {delivered: true, deposited: false}
```

The King must be able to work **without** Ritual A — it is an optional enhancement,
and nothing else in the Realm depends on it.

## delivers-never-deposits (the sacred rule, enforced structurally)

Ritual A cannot deposit, by construction:

- `RitualPort` bundles ONLY the capabilities Ritual A needs
  (`spawn_knight`, `command_knight`, `get_journey`, `route`, `execute_decision`)
  and carries **no** `mine`/`deposit`/`kg_add`/`kg_supersede` function — so a
  deposit is *impossible* within this flow.
- `RitualPort.__post_init__` and `_assert_no_deposit_capability` refuse a port
  that smuggles in any of those capability names (defense-in-depth).
- `ArchmaesterSynthesis.deposited` is always `False` — an invariant, not a flag.
- The synthesis text is **returned to the caller**, never persisted into
  `meta.json` (only pointers live there — the journey anti-verbosity guard).

Deposit of BATTLE-VALIDATED saber happens only in Ritual B (Phase 5).

## API

```python
import ritual

synth = ritual.ritual_a(journey_id, subject, refs=None,
                        port=ritual.REAL_RITUAL_PORT,
                        archmaester_agent="wrcp",
                        candidates=None, task_salons=None, cwd=None)
# refs: [{type: jira|confluence|gerrit|url|file|text, value: ...}]
# candidates: optional Steward-scored knights-index rows -> reuse-by-essence routing
# -> ArchmaesterSynthesis {subject, archmaester_session_id, synthesis_text,
#                          delivered: True, deposited: False}

prompt = ritual.build_investigation_prompt(subject, refs)
# deterministic; instructs: MemPalace-first, then refs, then discover; seek the
# Archimedean point; flag risks + unknowns; DELIVER not deposit. Fails loud on a
# malformed/unknown ref.
```

## Testability seam
All lower-layer calls go through the injectable `RitualPort`; unit tests drive
fakes — no real tmux/kiro. Production uses `REAL_RITUAL_PORT`.

## Tests

```
python3 ritual/tests/test_ritual.py       # bare runner
python3 -m pytest ritual/tests -q          # if pytest available
```

13 isolated tests: spawn-fresh when no candidates; route-for-reuse when candidates
supplied; prompt contains subject + each ref + "MemPalace first" + "Archimedean
point" + "risks/unknowns" + the explicit DELIVER-not-deposit directive; prompt
fail-loud on empty subject / empty ref value / unknown ref type; ritual_a
fail-loud on empty subject/journey; the no-deposit guard (port rejects a deposit
capability; ritual_a never triggers a deposit; deposited always false); synthesis
not persisted to meta.json.

## Module map

```
ritual/
├── __init__.py     public API
├── errors.py       RitualError / DepositForbiddenError (extend motor.MotorError)
├── synthesis.py    ArchmaesterSynthesis value object (deposited always False)
├── prompt.py       build_investigation_prompt (deterministic assembly)
├── ritual.py       ritual_a + RitualPort (deposit-free seam) + summon logic
├── README.md       this file
└── tests/test_ritual.py   13 isolated tests
```
