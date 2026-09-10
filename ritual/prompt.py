"""Build the Ritual A investigation prompt (Phase 4).

Deterministic assembly: same (subject, refs) -> same prompt. The prompt tells
the archmaester WHAT to wield and in what order (MemPalace first, then the given
refs, then discover related), to seek the Archimedean point, to flag risks and
unknowns, and — critically — to DELIVER the synthesis and NOT deposit anything
(deposit is Ritual B). The archmaester's actual reasoning is generative and
lives in the knight; this only frames the task.
"""

from __future__ import annotations

from typing import Any

from .errors import RitualError

# Reference types the King may hand the archmaester.
_KNOWN_REF_TYPES = {"jira", "confluence", "gerrit", "url", "file", "text"}

# The explicit non-deposit directive — asserted verbatim by the tests.
_DELIVER_NOT_DEPOSIT = (
    "DELIVER the synthesis to the King (do NOT deposit anything into the "
    "palace — your findings are HYPOTHESIS until battle proves them; the "
    "deposit happens only at journey end / Ritual B)."
)


def _format_ref(ref: dict[str, Any], index: int) -> str:
    """Render one reference line. Fail loud on an unknown/malformed ref."""
    if not isinstance(ref, dict):
        raise RitualError(f"ref #{index} must be an object {{type, value}}")
    ref_type = ref.get("type")
    value = ref.get("value")
    if ref_type not in _KNOWN_REF_TYPES:
        raise RitualError(
            f"ref #{index} has unknown type {ref_type!r}; "
            f"expected one of {sorted(_KNOWN_REF_TYPES)}"
        )
    if value is None or value == "":
        raise RitualError(f"ref #{index} ({ref_type}) has an empty value")
    return f"  - [{ref_type}] {value}"


def build_investigation_prompt(subject: str, refs: list[dict[str, Any]]) -> str:
    """Assemble a clear, bounded investigation instruction for the archmaester.

    Args:
        subject: the subject to investigate (one line is ideal).
        refs: list of {type: jira|confluence|gerrit|url|file|text, value: ...}.

    Raises RitualError on an empty subject or a malformed/unknown ref.
    """
    if not subject or not subject.strip():
        raise RitualError("subject must be a non-empty string")

    ref_lines = [_format_ref(ref, i) for i, ref in enumerate(refs or [])]
    refs_block = "\n".join(ref_lines) if ref_lines else "  (none supplied)"

    return (
        f"You are the Archmaester. Investigate the subject and found the "
        f"Archimedean point — the greatest available certainty.\n\n"
        f"SUBJECT: {subject.strip()}\n\n"
        f"REFERENCES (starting points):\n{refs_block}\n\n"
        "WIELD YOUR BOOKS IN THIS ORDER:\n"
        "  1. MemPalace search FIRST — what the Realm already knows "
        "(past validated saber) about this subject.\n"
        "  2. Then the references above (Jira / Confluence / Gerrit / URLs / "
        "files / the given text).\n"
        "  3. Then discover related material the refs point to.\n\n"
        "SYNTHESIZE:\n"
        "  - the ARCHIMEDEAN POINT (the point of greatest certainty available),\n"
        "  - the RISKS around it,\n"
        "  - the UNKNOWNS you could not resolve (be honest — you are not "
        "omniscient; distinguish what you proved, presume, and do not know).\n\n"
        f"{_DELIVER_NOT_DEPOSIT}\n"
    )
