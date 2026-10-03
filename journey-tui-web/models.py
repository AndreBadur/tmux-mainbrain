"""Pydantic response models for the journey-tui-web API.

Kept intentionally thin — the shapes mirror the round-6 sketch and the governed
cascade produced by :mod:`cascade`. All enrich fields are optional because a
node may be governed (present in meta.json) without any live scan data.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CascadeNode(BaseModel):
    """One node in the governed cascade tree.

    The identity fields (``session_id``, ``role``, ``agent``, ``tmux_session``,
    ``level``) come from the authoritative ``meta.json`` tree. The live fields
    (``alive``, ``context_pct``) are joined on ``session_id`` from the motor's
    ``scan_knights`` / ``context_of``.

    Two action flags drive the UI:
      * ``clickable``  — the ``tmux_session`` is LIVE now; clicking SWITCHES the
        terminal to it (no side effects).
      * ``revivable``  — the knight has a ``session_id`` but no live tmux
        session; clicking REVIVES it (motor ``resume_clean``: clear a dead lock,
        start a fresh tmux session via ``--resume-id``) then switches. The
        king's tmux session is resolved from live sessions by its id suffix, so
        a running king is ``clickable`` even when meta.json omits its name.
    """

    session_id: str
    role: str | None = None
    agent: str | None = None
    tmux_session: str | None = None
    level: int | None = None
    alive: bool = False
    context_pct: float | None = None
    clickable: bool = False
    revivable: bool = False
    children: list["CascadeNode"] = Field(default_factory=list)


class JourneyTree(BaseModel):
    """A single journey's governed tree (one root = the King)."""

    journey_id: str
    root: CascadeNode


class SwitchResult(BaseModel):
    """Result of a ``POST /api/switch/{session_id}``."""

    ok: bool
    target: str | None = None
    error: str | None = None


class Health(BaseModel):
    """Result of ``GET /api/health``."""

    ok: bool
    ttyd_up: bool


class ClientConfig(BaseModel):
    """Result of ``GET /api/config`` — runtime hints for the front-end.

    ``terminal_url`` is where the browser should point the embedded terminal
    iframe. It is computed server-side from ``JTW_PUBLIC_HOST`` so it is correct
    for localhost, LAN, or a DNS deployment WITHOUT hardcoding a host in the
    static page.
    """

    terminal_url: str
    auth_required: bool


# Resolve the forward reference for the self-referential ``children`` field.
CascadeNode.model_rebuild()
