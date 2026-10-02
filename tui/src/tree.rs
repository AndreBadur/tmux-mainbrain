//! Flatten the governed cascade forest into displayable, enriched rows.
//!
//! Joins the meta.json command tree (authoritative structure) with:
//!  * the live tmux session set — the AUTHORITY for runtime presence (the ●/○
//!    glyph), and
//!  * the scan_knights index — for `context_pct` and purpose ONLY.
//!
//! Bug fix: the ●/○/◌ glyph reflects tmux EXISTENCE, not motor liveness. motor
//! "stale" means "no recent conversation", which is NOT "offline". A node whose
//! tmux session exists is ALIVE regardless of motor liveness.

use std::collections::HashMap;

use crate::cascade::{Cascade, CascadeNode};
use crate::motor::Knight;
use crate::runtime::{self, LiveTmux};

/// Runtime-presence state of a node, derived from TMUX EXISTENCE (not motor
/// liveness).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LiveState {
    /// A tmux runtime exists for this knight → attachable now (● green).
    Alive,
    /// No tmux runtime, but a `session_id` exists → revivable (○).
    Offline,
    /// No session at all to attach or revive (◌). (Rare for governed nodes.)
    NotFound,
}

/// One flattened, enriched row ready to draw.
#[derive(Debug, Clone)]
pub struct TreeRow {
    /// Indentation depth (0 = journey header / group header).
    pub depth: u16,
    /// Declared level from meta.json (1=King,2=task,3=specialist), if any.
    pub level: Option<u8>,
    /// True for a journey/group header row (not a selectable knight).
    pub is_header: bool,
    /// Display label (role/agent, or the journey/group title for headers).
    pub label: String,
    /// The session id this row maps to (None for headers).
    pub session_id: Option<String>,
    /// tmux session name for the watch tab (M3), if known.
    pub tmux_session: Option<String>,
    pub state: LiveState,
    pub context_pct: Option<f64>,
    /// Short purpose/title from the live index (join on session_id), if any.
    pub purpose: Option<String>,
    /// Whether this node is the last child at its level (for tree glyphs).
    pub is_last: bool,
}

impl TreeRow {
    /// A row is selectable/clickable only if it carries a session id.
    pub fn is_selectable(&self) -> bool {
        !self.is_header && self.session_id.is_some()
    }
}

/// Build the full flattened row list from the governed cascade + the live tmux
/// set (runtime presence / glyph) + the scan_knights index (context%/purpose).
/// Order: one section per journey (header + its tree), then an "unlinked
/// sessions" section for live-tmux sessions not in any meta.json.
pub fn build_rows(cascade: &Cascade, index: &[Knight], live_tmux: &LiveTmux) -> Vec<TreeRow> {
    let by_sid: HashMap<&str, &Knight> =
        index.iter().map(|k| (k.session_id.as_str(), k)).collect();

    let mut rows: Vec<TreeRow> = Vec::new();

    for journey in &cascade.journeys {
        rows.push(TreeRow {
            depth: 0,
            level: None,
            is_header: true,
            label: format!("journey: {}", journey.journey_id),
            session_id: None,
            tmux_session: None,
            state: LiveState::NotFound,
            context_pct: None,
            purpose: None,
            is_last: false,
        });
        push_node(&journey.root, 1, true, &by_sid, live_tmux, &mut rows);
    }

    // Unlinked: live sessions in the motor index not governed by any meta.json.
    let mut unlinked: Vec<&Knight> = index
        .iter()
        .filter(|k| k.alive && !cascade.governed.contains(&k.session_id))
        .collect();
    unlinked.sort_by(|a, b| {
        a.agent_label()
            .cmp(b.agent_label())
            .then_with(|| a.session_id.cmp(&b.session_id))
    });

    if !unlinked.is_empty() {
        rows.push(TreeRow {
            depth: 0,
            level: None,
            is_header: true,
            label: format!("unlinked sessions ({})", unlinked.len()),
            session_id: None,
            tmux_session: None,
            state: LiveState::NotFound,
            context_pct: None,
            purpose: None,
            is_last: false,
        });
        let last_i = unlinked.len() - 1;
        for (i, k) in unlinked.iter().enumerate() {
            rows.push(TreeRow {
                depth: 1,
                level: None,
                is_header: false,
                label: k.agent_label().to_string(),
                session_id: Some(k.session_id.clone()),
                tmux_session: None,
                // Unlinked sessions come from the motor index (alive by filter);
                // they have no recorded tmux name, so treat as revivable-offline
                // unless motor says alive.
                state: if k.alive {
                    LiveState::Alive
                } else {
                    LiveState::Offline
                },
                context_pct: k.context_pct,
                purpose: k.purpose.clone(),
                is_last: i == last_i,
            });
        }
    }

    rows
}

fn push_node(
    node: &CascadeNode,
    depth: u16,
    is_last: bool,
    by_sid: &HashMap<&str, &Knight>,
    live_tmux: &LiveTmux,
    rows: &mut Vec<TreeRow>,
) {
    let live = by_sid.get(node.session_id.as_str());

    // Prefer the meta role/agent; fall back to the live index's agent.
    let label = node
        .role
        .clone()
        .or_else(|| node.agent.clone())
        .or_else(|| live.and_then(|k| k.agent.clone()))
        .unwrap_or_else(|| "<unknown>".to_string());

    // GLYPH AUTHORITY = tmux existence. Resolve the knight's tmux name (recorded
    // or derived) and check the live-tmux set. motor liveness only feeds ctx%.
    let tmux_name = runtime::resolve_tmux_name(
        node.tmux_session.as_deref(),
        &label,
        &node.session_id,
    );
    let state = if live_tmux.contains(&tmux_name) {
        LiveState::Alive // tmux runtime present → attachable now
    } else {
        LiveState::Offline // no runtime, but session_id exists → revivable
    };
    let context_pct = live.and_then(|k| k.context_pct);

    rows.push(TreeRow {
        depth,
        level: node.level,
        is_header: false,
        label,
        session_id: Some(node.session_id.clone()),
        tmux_session: node.tmux_session.clone(),
        state,
        context_pct,
        purpose: live.and_then(|k| k.purpose.clone()),
        is_last,
    });

    let n = node.children.len();
    for (i, child) in node.children.iter().enumerate() {
        push_node(child, depth + 1, i + 1 == n, by_sid, live_tmux, rows);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cascade::{CascadeNode, JourneyTree};

    fn node(sid: &str, role: &str, children: Vec<CascadeNode>) -> CascadeNode {
        CascadeNode {
            session_id: sid.to_string(),
            role: Some(role.to_string()),
            agent: None,
            tmux_session: None,
            level: None,
            children,
        }
    }

    fn knight(sid: &str, agent: &str, alive: bool, ctx: f64) -> Knight {
        Knight {
            session_id: sid.to_string(),
            agent: Some(agent.to_string()),
            alive,
            context_pct: Some(ctx),
            purpose: None,
            parent: None,
        }
    }

    fn cascade_with(root: CascadeNode) -> Cascade {
        let mut c = Cascade::default();
        let mut set = std::collections::HashSet::new();
        fn collect(n: &CascadeNode, s: &mut std::collections::HashSet<String>) {
            s.insert(n.session_id.clone());
            n.children.iter().for_each(|c| collect(c, s));
        }
        collect(&root, &mut set);
        c.governed = set;
        c.journeys.push(JourneyTree {
            journey_id: "j".to_string(),
            root,
        });
        c
    }

    #[test]
    fn glyph_reflects_tmux_existence_not_motor_liveness() {
        // The bug: an idle knight whose tmux is alive but motor says stale.
        let root = node(
            "king",
            "king",
            vec![node("arch", "archmaester", vec![])],
        );
        // Give the arch node a recorded tmux name.
        let mut cascade = cascade_with(root);
        cascade.journeys[0].root.children[0].tmux_session = Some("archmaester-tui".to_string());

        // Motor index says arch is NOT alive (stale) — the old code marked it offline.
        let index = vec![
            knight("king", "wise-king", false, 14.0),
            knight("arch", "archmaester", false, 9.6), // motor: stale/offline
        ];
        // But tmux says archmaester-tui EXISTS.
        let live = LiveTmux::from_names(["archmaester-tui".to_string()]);

        let rows = build_rows(&cascade, &index, &live);
        let arch = rows
            .iter()
            .find(|r| r.session_id.as_deref() == Some("arch"))
            .unwrap();
        assert_eq!(
            arch.state,
            LiveState::Alive,
            "tmux exists → ALIVE even though motor liveness is stale"
        );
        // ctx% still comes from the motor index.
        assert_eq!(arch.context_pct, Some(9.6));
    }

    #[test]
    fn no_tmux_but_session_present_is_offline_revivable() {
        let root = node("king", "king", vec![node("coder", "coder", vec![])]);
        let mut cascade = cascade_with(root);
        cascade.journeys[0].root.children[0].tmux_session = Some("coder-gone".to_string());
        let index = vec![knight("coder", "coder", false, 5.0)];
        // tmux set does NOT contain coder-gone → offline (revivable).
        let live = LiveTmux::from_names(["something-else".to_string()]);
        let rows = build_rows(&cascade, &index, &live);
        let coder = rows
            .iter()
            .find(|r| r.session_id.as_deref() == Some("coder"))
            .unwrap();
        assert_eq!(coder.state, LiveState::Offline, "no tmux → revivable-offline");
    }

    #[test]
    fn depth_reflects_hierarchy() {
        let root = node(
            "king",
            "king",
            vec![node("coder", "coder", vec![node("spec", "specialist", vec![])])],
        );
        // Give king + coder recorded tmux names that are "alive".
        let mut cascade = cascade_with(root);
        cascade.journeys[0].root.tmux_session = Some("king-tmux".to_string());
        cascade.journeys[0].root.children[0].tmux_session = Some("coder-tmux".to_string());
        let index = vec![
            knight("king", "wise-king", true, 14.0),
            knight("coder", "coder-tui", true, 6.7),
        ];
        let live = LiveTmux::from_names(["king-tmux".to_string(), "coder-tmux".to_string()]);
        let rows = build_rows(&cascade, &index, &live);
        // header + king + coder + spec
        assert_eq!(rows.len(), 4);
        assert!(rows[0].is_header);
        assert_eq!(rows[1].depth, 1); // king
        assert_eq!(rows[2].depth, 2); // coder
        assert_eq!(rows[3].depth, 3); // spec
        // spec has no tmux → offline (revivable), king/coder tmux present → alive.
        assert_eq!(rows[3].state, LiveState::Offline);
        assert_eq!(rows[1].state, LiveState::Alive);
    }

    #[test]
    fn unlinked_live_sessions_grouped_separately() {
        let root = node("king", "king", vec![]);
        let cascade = cascade_with(root);
        let index = vec![
            knight("king", "wise-king", true, 14.0),
            knight("stray", "wrcp", true, 3.0), // live in motor index, not governed
            knight("dead-stray", "wrcp", false, 3.0), // not alive → excluded
        ];
        let live = LiveTmux::default();
        let rows = build_rows(&cascade, &index, &live);
        let unlinked_header = rows.iter().find(|r| r.label.starts_with("unlinked"));
        assert!(unlinked_header.is_some());
        assert!(rows.iter().any(|r| r.session_id.as_deref() == Some("stray")));
        assert!(
            !rows.iter().any(|r| r.session_id.as_deref() == Some("dead-stray")),
            "dead unlinked sessions are not shown"
        );
    }
}
