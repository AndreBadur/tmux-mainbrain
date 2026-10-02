"""capacitate_from_repo — turn a repo URL into knight capability.

The Steward's on-demand forge: given a repo URL, clone it, CLASSIFY it into one
of three shapes, and PRODUCE the capacitation artifact — without spawning
anything (separation of concerns: this makes the material; spawning is a
separate motor call).

Shapes (detected structurally, verified against real repos):

  (1) POWER   — a kiro/Agent-Plugins power. Either the modern manifest
                ``plugin.json`` (name/version/description/keywords[]) [+ skills/
                + mcp.json], or the legacy ``POWER.md`` bundle (front-matter
                manifest + steering/). CHEAP-PATH forge: consolidate the bundle
                into ONE power file under ``artifacts/.kiro/powers/`` and forge a
                binding agent under ``artifacts/.kiro/agents/`` that loads it via
                ``resources: ["file://…"]`` — knowledge enters by FILE (part of
                the agent definition), never as a per-turn prompt.
  (2) AGENTS  — a repo carrying kiro agent personas under ``.kiro/agents/``
                (*.md / *.json). Forge a FILE-cheap ``ephemeral-<name>.md`` whose
                persona lives IN the agent file (definition-cheap).
  (3) CODE    — plain project, no kiro metadata. Clean REFUSAL: power-making from
                plain code is out of scope; nothing is forged.

THE STEWARD'S ESSENCE (cheap path): knowledge injected as a PROMPT costs tokens
every turn; knowledge loaded via a FILE (agent ``.md`` / ``resources`` / power
file) is part of the agent definition — cheap. This verb ALWAYS forges the FILE
bind; prompt injection is only a last-resort fallback. The JSON reports which
``bind`` path was used.

Design rules honoured: deterministic; null-tolerant (bad URL / no net / empty
repo -> CapacitateError -> clean CLI JSON, never a traceback); every subprocess
is bounded by an explicit timeout; the clone step is INJECTABLE so unit tests
drive the classifier + producers against local fixtures with NO network.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import CapacitateError

# Bounded clone timeout — the motor never hangs forever.
_CLONE_TIMEOUT = 120
# Max bytes of the consolidated power file. Generous — a real design-system
# power carries dozens of component specs; a resource FILE is paid once (cheap),
# so we keep the whole bundle rather than truncate useful knowledge. (Guards
# only against a pathological multi-MB repo.)
_POWER_MAX = 2_000_000


# --------------------------------------------------------------------------- #
# shape classification (pure — the testable core, no network)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Shape:
    POWER = "power"
    AGENTS = "agents"
    CODE = "code"


def detect_shape(repo_dir: Path) -> str:
    """Classify a cloned repo dir into POWER / AGENTS / CODE (guard-clause).

    Order matters: a power manifest wins over incidental ``.kiro`` content, and
    explicit agent personas win over plain code.
    """
    repo_dir = Path(repo_dir)
    if not repo_dir.is_dir():
        raise CapacitateError(f"not a directory: {repo_dir}")

    # (1) POWER — modern plugin.json OR legacy POWER.md bundle.
    if (repo_dir / "plugin.json").is_file():
        return Shape.POWER
    if (repo_dir / "POWER.md").is_file():
        return Shape.POWER

    # (2) AGENTS — kiro agent personas present.
    agents_dir = repo_dir / ".kiro" / "agents"
    if agents_dir.is_dir() and _has_agent_file(agents_dir):
        return Shape.AGENTS

    # (3) CODE — no kiro metadata.
    return Shape.CODE


def _has_agent_file(agents_dir: Path) -> bool:
    for pattern in ("*.md", "*.json"):
        for candidate in agents_dir.glob(pattern):
            if candidate.is_file():
                return True
    return False


# --------------------------------------------------------------------------- #
# clone (injectable seam for tests)
# --------------------------------------------------------------------------- #
def _git_shallow_clone(url: str, dest: Path, branch: Optional[str] = None) -> None:
    """Shallow-clone ``url`` into ``dest``. Bounded; translates failure."""
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, str(dest)]
    try:
        completed = subprocess.run(
            cmd, capture_output=True, timeout=_CLONE_TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise CapacitateError("git binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CapacitateError(f"git clone timed out after {_CLONE_TIMEOUT}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise CapacitateError(f"git clone failed: {stderr or 'unknown error'}")


CloneFn = Callable[[str, Path, Optional[str]], None]


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def capacitate_from_repo(url: str,
                         name: Optional[str] = None,
                         dest: Optional[str] = None,
                         journey: Optional[str] = None,
                         branch: Optional[str] = "main",
                         dry_run: bool = False,
                         clone_fn: Optional[CloneFn] = None) -> dict[str, Any]:
    """Clone ``url``, detect its shape, and PRODUCE the capacitation artifact.

    Does NOT spawn — it only makes the material. Returns a single JSON-able doc:
        {shape, bind, agent_file, power_file, cloned_to, attach_hint, …}

    Output placement (dev ruling): forged ephemerals are JOURNEY-TRACKED. The
    scratch git clone ALWAYS lands in ``.cap-forge/`` (gitignored); the forged
    artifacts land under the journey's ``artifacts/.kiro/{powers,agents}/`` so
    they are traceable + reactivatable. Resolution of the artifact root:
        --dest DIR      -> ``DIR`` (explicit override; artifacts go directly here)
        --journey <id>  -> ``journeys/<id>/artifacts``
        neither         -> ``.cap-forge/artifacts`` (no journey context; no crash)

    Args:
        url: the repo URL (or a local path, when ``clone_fn`` copies it).
        name: capability name (defaults to the repo's basename, sanitized).
        dest: explicit artifact root override (bypasses journey resolution).
        journey: journey id — artifacts land under journeys/<id>/artifacts.
        branch: branch to clone (default ``main``; None lets git pick default).
        dry_run: classify + report the intended artifact WITHOUT writing it.
        clone_fn: injectable clone (tests pass a local-copy fn; prod uses git).
    """
    if not url or not url.strip():
        raise CapacitateError("url must be a non-empty string")

    cap_name = _sanitize_name(name or _name_from_url(url))
    # Scratch clone ALWAYS in .cap-forge/ (gitignored); artifacts resolved apart.
    clone_dir = _default_forge_dir() / cap_name
    artifact_root = _resolve_artifact_root(dest, journey)
    cloner = clone_fn or (lambda u, d, b: _git_shallow_clone(u, d, b))

    # Fresh clone target (idempotent).
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        cloner(url, clone_dir, branch)
    except CapacitateError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacitateError(f"clone step failed: {exc}") from exc

    if not clone_dir.is_dir() or not any(clone_dir.iterdir()):
        raise CapacitateError(f"clone produced an empty repo for '{url}'")

    shape = detect_shape(clone_dir)

    if shape == Shape.POWER:
        result = _produce_power(clone_dir, cap_name, artifact_root, dry_run)
    elif shape == Shape.AGENTS:
        result = _produce_agent(clone_dir, cap_name, artifact_root, dry_run)
    else:
        # Shape (3) plain code: power-making from a non-power repo is out of
        # scope — refuse cleanly (the caller decides what to do instead).
        raise CapacitateError(
            f"'{url}' is not a kiro power and carries no .kiro/agents/ personas "
            f"(shape=code). Power-making from plain code is out of scope for "
            f"capacitate_from_repo; nothing forged."
        )

    result.update({"shape": shape, "cloned_to": str(clone_dir),
                   "name": cap_name, "dry_run": dry_run})
    return result


# --------------------------------------------------------------------------- #
# producers (one per shape) — ALWAYS prefer the FILE-cheap bind over injection
# --------------------------------------------------------------------------- #
def _produce_power(clone_dir: Path, name: str, artifact_root: Path,
                   dry_run: bool) -> dict[str, Any]:
    """Shape (1): the repo IS a power. Forge the CHEAP FILE bind.

    Consolidate the (possibly multi-file) power bundle into ONE loadable power
    file at ``<artifact_root>/.kiro/powers/ephemeral-power-<name>.md``, and forge
    a separate agent ``<artifact_root>/.kiro/agents/ephemeral-<name>.md`` that
    loads it by FILE via ``resources: ["file://<abs power path>"]`` — NO prompt
    injection (proven cheap-path, POC 2026-09-28). Returns the bind path used.
    """
    manifest = "plugin.json" if (clone_dir / "plugin.json").is_file() else "POWER.md"
    keywords = _power_keywords(clone_dir, manifest)

    power_dir = artifact_root / ".kiro" / "powers"
    agents_dir = artifact_root / ".kiro" / "agents"
    power_file = power_dir / f"ephemeral-power-{name}.md"
    agent_file = agents_dir / f"ephemeral-{name}.md"

    power_text = _consolidate_power(clone_dir, name, manifest, keywords)
    agent_text = _render_power_binding_agent(name, power_file, keywords)

    if not dry_run:
        power_dir.mkdir(parents=True, exist_ok=True)
        agents_dir.mkdir(parents=True, exist_ok=True)
        power_file.write_text(power_text, encoding="utf-8")
        agent_file.write_text(agent_text, encoding="utf-8")

    attach = (
        f"CHEAP FILE BIND: spawn `ephemeral-{name}` with "
        f"`--cwd {artifact_root}` (so V3 discovers the agent by name); the "
        f"agent loads the power via `resources: [\"file://{power_file}\"]` — the "
        f"knowledge is part of the agent definition, NOT a per-turn prompt. "
        f"Activates on keywords {keywords or '[…]'}."
    )
    return {"bind": "file-resources", "agent_file": str(agent_file),
            "power_file": str(power_file), "artifact_path": str(agent_file),
            "manifest": manifest, "keywords": keywords, "attach_hint": attach}


def _produce_agent(clone_dir: Path, name: str, artifact_root: Path,
                   dry_run: bool) -> dict[str, Any]:
    """Shape (2): adapt the repo's primary persona into a FILE-cheap agent.md.

    The persona lives IN the forged agent file (part of the definition, cheap),
    never injected as a prompt.
    """
    agents_dir = clone_dir / ".kiro" / "agents"
    source = _pick_primary_agent(agents_dir)
    persona = _read_agent_persona(source)
    forged_name = f"ephemeral-{name}"
    agent_md = _render_agent_md(forged_name, persona, source.name)

    out_dir = artifact_root / ".kiro" / "agents"
    agent_file = out_dir / f"{forged_name}.md"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        agent_file.write_text(agent_md, encoding="utf-8")
    attach = (
        f"CHEAP FILE BIND: spawn `{forged_name}` with `--cwd {artifact_root}` "
        f"— the persona is IN the agent file (definition-cheap), not injected. "
        f"(Discovery constraint: the agent must sit at the spawn cwd's "
        f".kiro/agents/, see rules doc §8.)"
    )
    return {"bind": "file-agent", "agent_file": str(agent_file),
            "power_file": None, "artifact_path": str(agent_file),
            "source_persona": source.name, "attach_hint": attach}


# --------------------------------------------------------------------------- #
# small helpers (pure)
# --------------------------------------------------------------------------- #
def _default_forge_dir() -> Path:
    from .paths import repo_root  # lazy import: avoid load-time cycle
    return repo_root() / ".cap-forge"


def _resolve_artifact_root(dest: Optional[str], journey: Optional[str]) -> Path:
    """Where forged ephemerals land (the ``.kiro/{powers,agents}`` parent).

    Resolution order (dev ruling — journey-tracked by default):
        dest      -> explicit override, used as-is.
        journey   -> ``journeys/<id>/artifacts`` (traceable, reactivatable).
        neither   -> ``.cap-forge/artifacts`` (no journey context; never crash).
    """
    if dest:
        return Path(dest).expanduser().resolve()
    if journey:
        try:
            from journey.paths import journey_dir  # lazy: journey is a sibling pkg
            return journey_dir(journey) / "artifacts"
        except Exception:  # noqa: BLE001 - fall back deterministically, never crash
            from .paths import repo_root
            return repo_root() / "journeys" / journey / "artifacts"
    return _default_forge_dir() / "artifacts"


def _name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return re.sub(r"\.git$", "", tail) or "capability"


def _sanitize_name(raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip().lower()).strip("-")
    return slug or "capability"


def _has(clone_dir: Path, rel: str) -> bool:
    return (clone_dir / rel).exists()


def _power_keywords(clone_dir: Path, manifest: str) -> list[str]:
    """Extract keywords[] from plugin.json or the POWER.md front matter."""
    try:
        if manifest == "plugin.json":
            doc = json.loads((clone_dir / "plugin.json").read_text("utf-8"))
            kws = doc.get("keywords")
            return [str(k) for k in kws] if isinstance(kws, list) else []
        # legacy POWER.md front matter (YAML-ish keywords list)
        text = (clone_dir / "POWER.md").read_text("utf-8", "replace")
        return _yaml_keywords(text)
    except (OSError, ValueError):
        return []


def _yaml_keywords(text: str) -> list[str]:
    """Parse a ``keywords:`` block from simple front matter without a YAML dep."""
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        if re.match(r"^\s*keywords\s*:", line):
            in_block = True
            # inline form: keywords: [a, b]
            inline = line.split(":", 1)[1].strip()
            if inline.startswith("["):
                return [k.strip().strip('"\'') for k in
                        inline.strip("[]").split(",") if k.strip()]
            continue
        if in_block:
            m = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if m:
                out.append(m.group(1).strip().strip('"\''))
            elif line.strip() and not line.startswith(" "):
                break  # next top-level key ends the block
    return out


def _pick_primary_agent(agents_dir: Path) -> Path:
    """Choose the repo's primary agent file (prefer .md; else .json; stable)."""
    md = sorted(agents_dir.glob("*.md"))
    if md:
        return md[0]
    js = sorted(agents_dir.glob("*.json"))
    if js:
        return js[0]
    raise CapacitateError(f"no agent file under {agents_dir}")


def _read_agent_persona(source: Path) -> str:
    """Return the persona body: markdown body, or a JSON agent's prompt field."""
    try:
        text = source.read_text("utf-8", "replace")
    except OSError as exc:
        raise CapacitateError(f"cannot read agent persona {source}: {exc}") from exc
    if source.suffix == ".json":
        try:
            doc = json.loads(text)
            return str(doc.get("prompt") or doc.get("description") or "").strip()
        except ValueError:
            return ""
    # .md — strip a leading front-matter block, keep the body.
    return _strip_front_matter(text).strip()


def _strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def _render_agent_md(forged_name: str, persona: str, source_name: str) -> str:
    body = persona or f"(adapted from {source_name})"
    desc = (f"Forged from a repo agent persona ({source_name}) via "
            f"capacitate_from_repo. Adapt/spawn as needed.")
    return (
        f"---\n"
        f"name: {forged_name}\n"
        f"description: {_yaml_quote(desc)}\n"
        f'tools: ["read", "write", "shell"]\n'
        f"includeMcpJson: false\n"
        f"permissions:\n"
        f"  rules:\n"
        f"    - capability: all\n"
        f"      effect: allow\n"
        f"---\n\n"
        f"# {forged_name}\n\n"
        f"{body}\n"
    )


def _consolidate_power(clone_dir: Path, name: str, manifest: str,
                       keywords: list[str]) -> str:
    """Fold a (possibly multi-file) power bundle into ONE loadable power file.

    The resulting single ``.md`` is what the binding agent loads via
    ``resources: file://`` (one file, cheaply, as part of the definition). It
    carries the manifest header + the bundle's guidance text (POWER.md body +
    steering/*.md, or plugin.json description + skills/*/SKILL.md), bounded so
    the agent definition stays lean.
    """
    parts: list[str] = [
        f"---\n"
        f"name: {name}\n"
        f"description: Power consolidated from a repo by capacitate_from_repo "
        f"(cheap file bind).\n"
        f"keywords: {keywords}\n"
        f"---\n",
        f"# Power: {name}\n",
    ]

    if manifest == "POWER.md":
        body = _strip_front_matter((clone_dir / "POWER.md").read_text("utf-8", "replace"))
        parts.append(body.strip())
        parts.append(_gather_markdown(clone_dir / "steering", "Steering"))
    else:  # plugin.json + skills/
        try:
            doc = json.loads((clone_dir / "plugin.json").read_text("utf-8"))
            parts.append(f"\n{doc.get('description', '').strip()}\n")
        except (OSError, ValueError):
            pass
        parts.append(_gather_markdown(clone_dir / "skills", "Skills"))

    text = "\n\n".join(p for p in parts if p and p.strip())
    return text[:_POWER_MAX] + ("\n" if not text.endswith("\n") else "")


def _gather_markdown(root: Path, heading: str, max_files: int = 300) -> str:
    """Concatenate the .md files under ``root`` (bounded), with light headers."""
    if not root.is_dir():
        return ""
    chunks: list[str] = [f"## {heading}"]
    count = 0
    for md in sorted(root.rglob("*.md")):
        if not md.is_file():
            continue
        try:
            content = md.read_text("utf-8", "replace").strip()
        except OSError:
            continue
        rel = md.relative_to(root)
        chunks.append(f"### {rel}\n\n{content}")
        count += 1
        if count >= max_files:
            chunks.append("… (additional files omitted for leanness)")
            break
    return "\n\n".join(chunks) if count else ""


def _render_power_binding_agent(name: str, power_file: Path,
                                keywords: list[str]) -> str:
    """Forge an agent that loads the power BY FILE via resources (cheap path)."""
    forged_name = f"ephemeral-{name}"
    kw_text = ", ".join(keywords) if keywords else name
    desc = (f"Forged by capacitate_from_repo - equipped with the '{name}' power "
            f"(loaded by FILE, not injected).")
    return (
        f"---\n"
        f"name: {forged_name}\n"
        f"description: {_yaml_quote(desc)}\n"
        f'tools: ["read", "write", "shell"]\n'
        f"includeMcpJson: false\n"
        f"resources:\n"
        f"  - {_yaml_quote('file://' + str(power_file))}\n"
        f"permissions:\n"
        f"  rules:\n"
        f"    - capability: all\n"
        f"      effect: allow\n"
        f"---\n\n"
        f"# {forged_name}\n\n"
        f"You are equipped with the **{name}** power, loaded into your context as "
        f"a resource FILE (part of your definition - no per-turn prompt cost). "
        f"Use its guidance to serve tasks about: {kw_text}.\n"
    )


def _yaml_quote(value: str) -> str:
    """Double-quote a YAML scalar safely (escape backslashes and quotes).

    Front-matter values that contain ``:``, ``[``, ``#`` etc. MUST be quoted or
    the YAML parser rejects the agent file (verified: an unquoted description
    with a colon fails with AgentFileFormatError).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

