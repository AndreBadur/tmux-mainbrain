"""Ritual B — end-of-journey curation of validated spoils (Phase 5, FINAL).

The COUNTERPART to Ritual A. Ritual A delivered hypothesis and NEVER deposited;
Ritual B collects battle feedback and DEPOSITS the validated saber. It composes
existing layers — journey (collect knight reports), motor.mine (the ONLY deposit
mechanism in the Realm), steward.keep/retire/delete (lifecycle) — and adds the
one new piece: provenance-tagged curation.

THE TWO MOMENTS, NEVER CONFUSED:
  * Ritual A (Phase 4): investigate -> deliver -> ``deposited: false``.
  * Ritual B (here):    report -> validate -> deposit (validated saber).
The provenance guard (every drawer MUST carry an explicit ``validated`` bool) is
enforced BEFORE any deposit, so untested hypothesis can never masquerade as
battle-proven truth.

Deterministic orchestration: the Archmaester's validated/hypothesis JUDGMENT is
an INPUT (the ``curated_drawers`` it produced), never invented here.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import journey
import motor
import steward

from .errors import CurationError, DepositError
from .models import (
    DepositResult,
    KnightReport,
    LifecycleResult,
    RitualBResult,
    SaberDrawer,
    drawer_from_dict,
)

# The closing prompt each knight receives (deterministic; the reasoning is the
# knight's). Structured so a peek can be split into worked/failed if the knight
# uses the markers — but we keep the raw text too.
_SPOILS_PROMPT = (
    "The journey is ending. Bring your spoils: report concisely what WORKED "
    "and what FAILED in your work this journey. Use exactly two lines:\n"
    "WORKED: <one line>\nFAILED: <one line>"
)

_VALID_FATES = {"keep", "retire", "delete"}


@dataclass(frozen=True)
class CurationPort:
    """The (composed) surface Ritual B needs. Injectable for isolated tests."""

    # journey
    command_knight: Callable[..., str]
    get_journey: Callable[..., dict[str, Any]]
    # motor (the deposit mechanism)
    mine: Callable[..., dict[str, Any]]
    # steward lifecycle
    keep: Callable[..., dict[str, Any]]
    retire: Callable[..., dict[str, Any]]
    delete: Callable[..., dict[str, Any]]
    # steward index refresh (for retire, which mutates an index doc)
    scan_knights: Callable[..., dict[str, Any]]


REAL_CURATION_PORT = CurationPort(
    command_knight=journey.command_knight,
    get_journey=journey.get_journey,
    mine=motor.mine,
    keep=steward.keep,
    retire=steward.retire,
    delete=steward.delete,
    scan_knights=motor.scan_knights,
)


# --------------------------------------------------------------------------- #
# 1. collect_spoils
# --------------------------------------------------------------------------- #
def collect_spoils(journey_id: str,
                   port: CurationPort = REAL_CURATION_PORT) -> list[KnightReport]:
    """Order each knight to report what worked / what failed.

    Returns a KnightReport per knight. Reports are TRANSIENT — returned to the
    caller, NEVER persisted into meta.json (anti-verbosity).
    """
    if not journey_id or not journey_id.strip():
        raise CurationError("journey_id is required")

    doc = port.get_journey(journey_id)
    reports: list[KnightReport] = []
    for knight in doc.get("knights", []):
        role = knight.get("role")
        raw = port.command_knight(journey_id, role, _SPOILS_PROMPT)
        worked, failed = _split_report(raw)
        reports.append(KnightReport(
            role=role, session_id=knight.get("session_id"),
            worked=worked, failed=failed))
    return reports


def _split_report(raw: str) -> tuple[str, str]:
    """Parse the WORKED:/FAILED: lines from a peek; fall back to the raw text."""
    worked, failed = "", ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("WORKED:"):
            worked = stripped[len("WORKED:"):].strip()
        elif upper.startswith("FAILED:"):
            failed = stripped[len("FAILED:"):].strip()
    if not worked and not failed:
        worked = (raw or "").strip()  # keep the raw report if unmarked
    return worked, failed


# --------------------------------------------------------------------------- #
# 2. curate_saber
# --------------------------------------------------------------------------- #
def curate_saber(subject: str,
                 synthesis: Any,
                 reports: list[KnightReport],
                 curated_drawers: list[dict[str, Any]],
                 port: CurationPort = REAL_CURATION_PORT) -> list[SaberDrawer]:
    """Validate the Archmaester's curated drawers (provenance is MANDATORY).

    The Archmaester (generative) has already classified each drawer as
    battle-validated (``validated: true``) or hypothesis (``validated: false``)
    using the Ritual A ``synthesis`` + the battle ``reports``. This function
    does NOT invent that classification — it VALIDATES the structure and FAILS
    LOUD (ProvenanceError) on any drawer missing the explicit flag.

    ``subject``/``synthesis``/``reports`` are accepted for interface symmetry and
    caller traceability; the judgment they informed lives in ``curated_drawers``.
    """
    if not subject or not subject.strip():
        raise CurationError("subject is required")
    return [drawer_from_dict(raw) for raw in (curated_drawers or [])]


# --------------------------------------------------------------------------- #
# 3. deposit_saber  (the ONLY deposit in the whole Realm)
# --------------------------------------------------------------------------- #
def deposit_saber(drawers: list[SaberDrawer],
                  port: CurationPort = REAL_CURATION_PORT,
                  dry_run: bool = False,
                  agent: str = "archmaester") -> DepositResult:
    """Deposit curated drawers into palace/archmaester/ via motor.mine.

    Each drawer is written as a clean turn file (the motor's mine preprocesses
    convos) under a temp path, then mined into its ``wing``/``room`` with the
    provenance flag recorded. ``dry_run`` records what WOULD be deposited
    without calling mine. This is the ONLY place in the Realm that deposits.
    """
    planned: list[dict[str, Any]] = []
    for drawer in drawers or []:
        if not isinstance(drawer, SaberDrawer):
            raise CurationError("deposit_saber expects SaberDrawer instances "
                                "(run curate_saber first)")
        record = {"wing": drawer.wing, "room": drawer.room,
                  "validated": drawer.validated,
                  "provenance_note": drawer.provenance_note}
        if dry_run:
            record["dry_run"] = True
            planned.append(record)
            continue
        record["result"] = _mine_drawer(port, drawer, agent)
        planned.append(record)

    return DepositResult(dry_run=dry_run, deposited=tuple(planned))


def _mine_drawer(port: CurationPort, drawer: SaberDrawer,
                 agent: str) -> dict[str, Any]:
    """Write the drawer content as a clean turn file and mine it. Bounded."""
    tmp_path: Optional[str] = None
    try:
        # A minimal clean USER/AGENT convo carrying the provenance in-band.
        provenance = "validated" if drawer.validated else "hypothesis"
        body = (
            f"### USER\nCurated saber for {drawer.wing}/{drawer.room} "
            f"(provenance: {provenance}).\n\n"
            f"### AGENT\n{drawer.content.strip()}\n"
            f"\n[provenance] validated={str(drawer.validated).lower()}"
        )
        if drawer.provenance_note:
            body += f"; note={drawer.provenance_note}"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".saber.jsonl", delete=False
        ) as handle:
            # motor.mine auto-detects transcript shape; we hand it a v3-style
            # user/assistant convo so preprocessing yields clean turns.
            import json
            handle.write(json.dumps({"payload": {"type": "user",
                         "content": f"Curated saber for {drawer.wing}/{drawer.room}"}}) + "\n")
            handle.write(json.dumps({"payload": {"type": "assistant",
                         "operationType": "Say",
                         "content": body}}) + "\n")
            tmp_path = handle.name

        return port.mine(tmp_path, drawer.wing, drawer.room, agent=agent)
    except motor.MotorError as exc:
        raise DepositError(
            f"deposit of {drawer.wing}/{drawer.room} failed: {exc}") from exc
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# 4. arbitrate_lifecycle
# --------------------------------------------------------------------------- #
def arbitrate_lifecycle(journey_id: str,
                        decisions: list[dict[str, Any]],
                        port: CurationPort = REAL_CURATION_PORT,
                        index: Optional[dict[str, Any]] = None
                        ) -> LifecycleResult:
    """Apply the King's per-knight fate via the EXISTING steward lifecycle ops.

    ``decisions`` = [{session_id, fate: keep|retire|delete, force?, confirm?}].
    delete stays GATED: its confirm/force flags are passed through explicitly and
    never bypassed. retire mutates an index doc (refreshed via scan if not given).
    """
    applied: list[dict[str, Any]] = []
    working_index = index

    for decision in decisions or []:
        session_id = decision.get("session_id")
        fate = decision.get("fate")
        if fate not in _VALID_FATES:
            raise CurationError(
                f"invalid fate {fate!r} for {session_id}; "
                f"expected one of {sorted(_VALID_FATES)}")
        if not session_id:
            raise CurationError(f"lifecycle decision missing session_id: {decision}")

        if fate == "keep":
            out = port.keep(session_id)
        elif fate == "retire":
            if working_index is None:
                working_index = port.scan_knights()
            out = port.retire(session_id, working_index)
            out = {"session_id": session_id, "lifecycle": "retire"}
        else:  # delete — pass the gate flags through explicitly, never bypass.
            out = port.delete(
                session_id,
                force=bool(decision.get("force", False)),
                confirm=bool(decision.get("confirm", False)),
            )
        applied.append({"session_id": session_id, "fate": fate, "result": out})

    return LifecycleResult(applied=tuple(applied))


# --------------------------------------------------------------------------- #
# 5. ritual_b — the full guaranteed closing flow
# --------------------------------------------------------------------------- #
def ritual_b(journey_id: str,
             subject: str,
             synthesis: Any,
             curated_drawers: list[dict[str, Any]],
             lifecycle_decisions: list[dict[str, Any]],
             port: CurationPort = REAL_CURATION_PORT,
             dry_run: bool = False) -> RitualBResult:
    """Compose collect_spoils -> curate_saber -> deposit_saber -> arbitrate.

    A trivial journey with no ``curated_drawers`` deposits nothing but STILL
    collects reports and arbitrates lifecycle (the closing step is guaranteed).
    Returns a RitualBResult summary. Reports/synthesis are RETURNED, never
    persisted into meta.json.
    """
    if not journey_id or not journey_id.strip():
        raise CurationError("journey_id is required")

    reports = collect_spoils(journey_id, port=port)

    drawers = curate_saber(subject, synthesis, reports, curated_drawers or [],
                           port=port)
    deposit = deposit_saber(drawers, port=port, dry_run=dry_run)

    lifecycle = arbitrate_lifecycle(journey_id, lifecycle_decisions or [],
                                    port=port)

    return RitualBResult(reports=tuple(reports), deposit=deposit,
                         lifecycle=lifecycle)
