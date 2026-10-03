"""Unit tests for cascade.py — tree build + enrichment join, pure (no I/O)."""
import sys
from pathlib import Path

# Make the app package importable when pytest runs from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cascade as c  # noqa: E402
from models import CascadeNode  # noqa: E402


def _count(node: CascadeNode) -> int:
    return 1 + sum(_count(ch) for ch in node.children)


# --- new format: nest under parent ------------------------------------------

def test_new_format_nests_under_parent():
    meta = {
        "journey_id": "j-new",
        "king_session": "sess_old_king",
        "new_king_session": "sess_king",
        "tree_root": {"role": "king", "session_id": "sess_king"},
        "knights": [
            {"role": "steward", "session_id": "sess_steward", "parent": "sess_king", "level": 2},
            {"role": "coder", "session_id": "sess_coder", "parent": "sess_king", "level": 2},
            {"role": "specialist", "session_id": "sess_spec", "parent": "sess_coder", "level": 3},
        ],
    }
    tree = c.parse_journey(meta)
    assert tree is not None
    root = tree.root
    assert root.session_id == "sess_king"
    assert root.role == "king"
    assert len(root.children) == 2
    coder = next(n for n in root.children if n.session_id == "sess_coder")
    assert len(coder.children) == 1
    assert coder.children[0].session_id == "sess_spec"
    assert _count(root) == 4


# --- old format: flat under king --------------------------------------------

def test_old_format_flattens_under_king():
    meta = {
        "journey_id": "j-old",
        "king_session": "sess_king_old",
        "knights": [
            {"role": "steward", "session_id": "sess_a"},
            {"role": "archmaester", "session_id": "sess_b"},
        ],
    }
    tree = c.parse_journey(meta)
    assert tree is not None
    root = tree.root
    assert root.session_id == "sess_king_old"
    assert len(root.children) == 2
    assert all(not ch.children for ch in root.children)


# --- dangling parent is not dropped -----------------------------------------

def test_dangling_parent_hangs_under_root():
    meta = {
        "journey_id": "j-dangle",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "coder", "session_id": "sess_coder", "parent": "sess_ghost", "level": 2},
        ],
    }
    tree = c.parse_journey(meta)
    assert tree is not None
    assert any(ch.session_id == "sess_coder" for ch in tree.root.children)


# --- tui filter: journey level ----------------------------------------------

def test_disabled_journey_is_filtered_out():
    enabled = {
        "journey_id": "j-on", "tui": "ENABLED",
        "tree_root": {"session_id": "sess_on_king"},
        "knights": [{"role": "coder", "session_id": "sess_on_c", "parent": "sess_on_king", "level": 2}],
    }
    disabled = {
        "journey_id": "j-off", "tui": "DISABLED",
        "tree_root": {"session_id": "sess_off_king"},
        "knights": [{"role": "coder", "session_id": "sess_off_c", "parent": "sess_off_king", "level": 2}],
    }
    missing = {
        "journey_id": "j-missing", "king_session": "sess_miss_king",
        "knights": [{"role": "steward", "session_id": "sess_miss_s"}],
    }
    trees = c.build_cascade([enabled, disabled, missing], live={})
    ids = [t.journey_id for t in trees]
    assert "j-on" in ids
    assert "j-missing" in ids  # missing tag = enabled
    assert "j-off" not in ids


def test_tui_tag_case_insensitive():
    assert c._is_tui_enabled({"tui": "ENABLED"})
    assert c._is_tui_enabled({"tui": "enabled"})
    assert c._is_tui_enabled({})  # missing = enabled
    assert not c._is_tui_enabled({"tui": "DISABLED"})
    assert not c._is_tui_enabled({"tui": "retired"})


# --- per-knight tui filter --------------------------------------------------

def test_disabled_knight_is_hidden():
    meta = {
        "journey_id": "j-knight-filter",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "coder", "session_id": "sess_live", "parent": "sess_king", "level": 2},
            {"role": "coder", "tui": "DISABLED", "session_id": "sess_dead", "parent": "sess_king", "level": 2},
        ],
    }
    tree = c.parse_journey(meta)
    assert tree is not None
    ids = {ch.session_id for ch in tree.root.children}
    assert "sess_live" in ids
    assert "sess_dead" not in ids


# --- enrichment join + clickable contract -----------------------------------

def test_enrichment_join_and_clickable():
    meta = {
        "journey_id": "j-enrich",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "steward", "session_id": "sess_s", "tmux_session": "steward-tui", "parent": "sess_king", "level": 2},
            {"role": "coder", "session_id": "sess_c", "tmux_session": "coder-tui", "parent": "sess_king", "level": 2},
            {"role": "ghost", "session_id": "sess_g", "parent": "sess_king", "level": 2},
        ],
    }
    live = {
        "sess_s": {"alive": True, "context_pct": 12.5},
        "sess_c": {"alive": False, "context_pct": 57.7},
        # sess_g absent from live entirely
    }
    # Only steward-tui is a live tmux session. coder-tui exists in meta but is
    # NOT a running tmux session (idle/gone), so it must be unclickable even
    # though it has a tmux_session field.
    live_tmux = frozenset({"steward-tui"})
    trees = c.build_cascade([meta], live, live_tmux)
    root = trees[0].root
    by_id = {n.session_id: n for n in root.children}

    s = by_id["sess_s"]
    assert s.alive is True            # motor display signal preserved
    assert s.context_pct == 12.5
    assert s.clickable is True        # tmux session is live -> switchable
    assert s.revivable is False       # already live, nothing to revive

    cc = by_id["sess_c"]
    assert cc.alive is False
    assert cc.context_pct == 57.7
    assert cc.clickable is False      # has tmux_session, but no live tmux pane
    assert cc.revivable is True       # resumable session id -> revive on click

    g = by_id["sess_g"]
    assert g.alive is False
    assert g.context_pct is None
    assert g.clickable is False       # no live tmux pane
    assert g.revivable is True        # still has a session id -> revivable


def test_empty_session_id_is_not_revivable():
    """A retired knight with an empty session_id can be neither switched nor
    revived (nothing to resume)."""
    meta = {
        "journey_id": "j-retired",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "coder", "session_id": "", "tmux_session": "coder-tui", "parent": "sess_king", "level": 2},
        ],
    }
    trees = c.build_cascade([meta], live={}, live_tmux=frozenset())
    node = trees[0].root.children[0]
    assert node.clickable is False
    assert node.revivable is False


def test_king_without_tmux_resolved_by_id_suffix():
    """The king's meta.json has no tmux_session; if a live tmux session carries
    its id suffix (king-354dd79f), it resolves and becomes clickable."""
    meta = {
        "journey_id": "j-king",
        "tree_root": {"role": "king", "session_id": "sess_354dd79f-5a9e-495a-a019-463ed57dfa2b"},
        "knights": [],
    }
    live = {"sess_354dd79f-5a9e-495a-a019-463ed57dfa2b": {"alive": True, "context_pct": 14.3}}
    trees = c.build_cascade([meta], live, frozenset({"king-354dd79f", "steward-tui"}))
    root = trees[0].root
    assert root.tmux_session == "king-354dd79f"   # resolved by suffix
    assert root.clickable is True
    assert root.revivable is False


def test_king_suffix_ambiguous_does_not_resolve():
    """Two live sessions sharing the suffix must NOT auto-resolve (avoid
    attaching to the wrong one)."""
    meta = {
        "journey_id": "j-king-amb",
        "tree_root": {"role": "king", "session_id": "sess_354dd79f-aaaa"},
        "knights": [],
    }
    live_tmux = frozenset({"king-354dd79f", "other-354dd79f"})
    trees = c.build_cascade([meta], live={}, live_tmux=live_tmux)
    root = trees[0].root
    assert root.tmux_session is None   # ambiguous -> unresolved
    assert root.clickable is False


def test_clickable_decoupled_from_motor_alive():
    """The whole point of the fix: an idle knight (motor alive=False) whose
    tmux session is live must be CLICKABLE."""
    meta = {
        "journey_id": "j-idle",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "steward", "session_id": "sess_idle", "tmux_session": "steward-tui", "parent": "sess_king", "level": 2},
        ],
    }
    live = {"sess_idle": {"alive": False, "context_pct": 15.1}}  # motor: stale
    trees = c.build_cascade([meta], live, frozenset({"steward-tui"}))
    node = trees[0].root.children[0]
    assert node.alive is False        # motor still says idle
    assert node.clickable is True     # but it IS attachable


def test_no_live_tmux_makes_rows_revivable_not_clickable():
    meta = {
        "journey_id": "j-dark",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "coder", "session_id": "sess_c", "tmux_session": "coder-tui", "parent": "sess_king", "level": 2},
        ],
    }
    live = {"sess_c": {"alive": True, "context_pct": 5.0}}
    # tmux reports nothing live (default empty set).
    trees = c.build_cascade([meta], live)
    node = trees[0].root.children[0]
    assert node.clickable is False     # no live tmux pane -> cannot switch
    assert node.revivable is True      # but has a session id -> revive on click


def test_resolve_node_finds_nested():
    meta = {
        "journey_id": "j-resolve",
        "tree_root": {"session_id": "sess_king"},
        "knights": [
            {"role": "coder", "session_id": "sess_c", "tmux_session": "coder-tui", "parent": "sess_king", "level": 2},
            {"role": "sub", "session_id": "sess_sub", "tmux_session": "sub-tui", "parent": "sess_c", "level": 3},
        ],
    }
    trees = c.build_cascade([meta], live={})
    node = c.resolve_node(trees, "sess_sub")
    assert node is not None
    assert node.tmux_session == "sub-tui"
    assert c.resolve_node(trees, "sess_nope") is None


def test_live_index_from_scan_projection():
    scan = {
        "count": 2,
        "knights": [
            {"session_id": "sess_a", "alive": True, "context_pct": 3.2, "agent": "x"},
            {"session_id": "sess_b", "alive": False, "context_pct": None},
            {"agent": "no-sid"},  # dropped
        ],
    }
    idx = c.live_index_from_scan(scan)
    assert idx["sess_a"] == {"alive": True, "context_pct": 3.2}
    assert idx["sess_b"] == {"alive": False, "context_pct": None}
    assert len(idx) == 2
