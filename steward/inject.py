"""Birth by curated injection — §3.x (Phase 3).

The DECISION of what is essential (lean) vs the whole salon (deep) is the
Steward's judgment — this module accepts already-selected content and:
  * reads file/dir sources (a repo's .kiro/agents + steering, etc.),
  * accepts inline MemPalace cuttings the Steward supplies,
  * assembles ONE bounded injection payload,
  * fails loud on a missing source path (never silently skips),
  * delivers it via the motor's unlimited-payload path.

Deterministic assembly: same sources + same dose -> same payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from . import config
from .errors import InjectionError, InjectionTooLargeError, SourceNotFoundError

# Dose presets — the Steward decides which; both just bound assembly here.
_DOSE_LEAN = "lean"
_DOSE_DEEP = "deep"
_VALID_DOSES = {_DOSE_LEAN, _DOSE_DEEP}

# File extensions worth injecting from a source directory (curated knowledge).
_INJECTABLE_SUFFIXES = {".md", ".txt", ".rst", ".yaml", ".yml", ".json"}


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InjectionError(f"cannot read source {path}: {exc}") from exc


def _gather_from_path(path: Path, deep: bool) -> list[tuple[str, str]]:
    """Return (label, text) blocks from a file or directory source.

    A missing path FAILS LOUD (SourceNotFoundError). For a directory, only
    injectable-suffix files are read; ``deep`` recurses, ``lean`` reads only the
    top level (the Steward curates the rest).
    """
    if not path.exists():
        raise SourceNotFoundError(f"injection source does not exist: {path}")
    if path.is_file():
        return [(str(path), _read_file(path))]

    blocks: list[tuple[str, str]] = []
    globber = path.rglob("*") if deep else path.glob("*")
    for child in sorted(globber):
        if child.is_file() and child.suffix in _INJECTABLE_SUFFIXES:
            blocks.append((str(child), _read_file(child)))
    return blocks


def assemble_payload(sources: list[Any], dose: str = _DOSE_LEAN) -> str:
    """Assemble the bounded injection payload from sources (deterministic).

    ``sources`` items may be:
      * a str/Path to a file or directory, OR
      * a dict {"label": str, "text": str} for inline MemPalace cuttings.

    Size is accumulated INCREMENTALLY and the assembly ABORTS with
    InjectionTooLargeError as soon as the running total exceeds the cap
    (review iter-1 C4 MINOR) — a mis-scoped ``deep`` source cannot read a whole
    tree into memory before rejection. Raises SourceNotFoundError on a missing
    path.
    """
    if dose not in _VALID_DOSES:
        raise InjectionError(f"invalid dose '{dose}'; use 'lean' or 'deep'")
    deep = dose == _DOSE_DEEP
    cap = config.max_injection_bytes()

    header = (
        "# Curated knowledge injection (Steward, dose="
        f"{dose})\n# You are being trained with the following curated context.\n"
    )
    running = len(header.encode("utf-8"))
    rendered_blocks: list[str] = []

    def _add_block(label: str, text: str) -> None:
        nonlocal running
        block = f"## SOURCE: {label}\n{text.strip()}"
        running += len(block.encode("utf-8")) + 2  # +2 for the join separator
        if running > cap:
            raise InjectionTooLargeError(
                f"injection payload exceeds cap {cap} bytes while assembling "
                f"'{label}' (dose={dose}); curate fewer cuttings (lean) or raise "
                "the cap"
            )
        rendered_blocks.append(block)

    for source in sources or []:
        if isinstance(source, dict):
            label = source.get("label", "cutting")
            text = source.get("text", "")
            if not text:
                raise InjectionError(f"inline source '{label}' has empty text")
            _add_block(label, text)
        elif isinstance(source, (str, Path)):
            for label, text in _gather_from_path(Path(source).expanduser(), deep):
                _add_block(label, text)
        else:
            raise InjectionError(f"unsupported source type: {type(source)!r}")

    if not rendered_blocks:
        raise InjectionError("no injectable content assembled from sources")

    payload = header + "\n" + "\n\n".join(rendered_blocks) + "\n"
    # Final exact check (the incremental total is a close upper bound).
    size = len(payload.encode("utf-8"))
    if size > cap:
        raise InjectionTooLargeError(
            f"injection payload {size} bytes exceeds cap {cap} (dose={dose})"
        )
    return payload


def inject_knowledge(tmux_session: str,
                     sources: list[Any],
                     dose: str = _DOSE_LEAN,
                     port: Optional[Any] = None) -> dict[str, Any]:
    """Assemble a bounded payload and deliver it into a knight at birth.

    ``port`` is the motor deliver seam (defaults to the real motor). Returns
    {tmux_session, dose, bytes, blocks}.
    """
    from .execute import REAL_STEWARD_PORT  # lazy: avoid import cycle

    active_port = port or REAL_STEWARD_PORT
    if not tmux_session:
        raise InjectionError("tmux_session is required for injection")

    payload = assemble_payload(sources, dose=dose)
    active_port.deliver(tmux_session, payload)
    return {
        "tmux_session": tmux_session,
        "dose": dose,
        "bytes": len(payload.encode("utf-8")),
        "blocks": payload.count("## SOURCE:"),
    }
