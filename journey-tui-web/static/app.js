"use strict";

// journey-tui-web front-end. Read-mostly: poll /api/cascade every 3s and
// re-render the sidebar tree. Clicking a clickable (alive + has tmux) row POSTs
// /api/switch/{sid}; the single <iframe> is NEVER reloaded — only tmux switches
// underneath, so scrollback is preserved.

const POLL_MS = 3000;
const treesEl = document.getElementById("trees");
const statusEl = document.getElementById("status");

// The currently selected session_id (survives re-renders for highlight).
let selectedId = null;

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function renderNode(node) {
  const li = el("li", "node");

  const row = el("div", "row");
  const clickable = node.clickable === true;   // live tmux -> switch
  const revivable = node.revivable === true;   // dead but resumable -> revive
  const interactive = clickable || revivable;
  if (interactive) {
    row.classList.add("clickable");
  } else {
    row.classList.add("dim");
  }
  if (node.session_id === selectedId) row.classList.add("selected");

  // Dot color = attachability: green = live (switch), amber = revivable
  // (start then switch), grey = nothing to attach. A pulsing ring = actively
  // working (motor alive), a non-conflicting secondary cue.
  let dotCls = "dot";
  if (clickable) dotCls += " attachable";
  else if (revivable) dotCls += " revivable";
  if (node.alive) dotCls += " working";
  const dot = el("span", dotCls);
  row.appendChild(dot);

  const role = el("span", "role", node.role || "?");
  row.appendChild(role);

  const label = node.agent ? ` ${node.agent}` : "";
  if (label) row.appendChild(el("span", "agent", label));

  if (node.context_pct !== null && node.context_pct !== undefined) {
    row.appendChild(el("span", "ctx", `${node.context_pct.toFixed(1)}%`));
  }

  if (clickable) {
    const working = node.alive ? " (working)" : " (idle)";
    row.title = `switch terminal to ${node.tmux_session}${working}`;
    row.addEventListener("click", () => switchTo(node, false));
  } else if (revivable) {
    row.title = "offline — click to revive (start its tmux session) and attach";
    row.addEventListener("click", () => switchTo(node, true));
  } else {
    row.title = "no resumable session to attach";
  }

  li.appendChild(row);

  if (Array.isArray(node.children) && node.children.length > 0) {
    const ul = el("ul");
    for (const child of node.children) ul.appendChild(renderNode(child));
    li.appendChild(ul);
  }
  return li;
}

function renderTrees(trees) {
  treesEl.textContent = "";
  if (!Array.isArray(trees) || trees.length === 0) {
    treesEl.appendChild(el("div", "status", "no governed journeys"));
    return;
  }
  for (const tree of trees) {
    treesEl.appendChild(el("div", "journey", tree.journey_id));
    const ul = el("ul", "tree");
    ul.appendChild(renderNode(tree.root));
    treesEl.appendChild(ul);
  }
}

async function switchTo(node, reviving) {
  selectedId = node.session_id;
  clearConsoleActive();
  // Immediate visual feedback; the poll will reconcile.
  document.querySelectorAll(".row.selected").forEach((r) => r.classList.remove("selected"));
  const label = node.tmux_session || node.role || node.session_id;
  statusEl.textContent = reviving
    ? `reviving ${label}… (starting tmux + kiro, may take a few seconds)`
    : `switching to ${label}…`;
  try {
    const res = await fetch(`/api/switch/${encodeURIComponent(node.session_id)}`, {
      method: "POST",
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      const err = body.error || body.detail || `HTTP ${res.status}`;
      statusEl.textContent = `${reviving ? "revive" : "switch"} failed: ${err}`;
      return;
    }
    statusEl.textContent = `showing ${body.target}`;
    // A revive changed the tmux landscape — refresh the tree promptly so the
    // row flips from amber (revivable) to green (live).
    if (reviving) poll();
  } catch (e) {
    statusEl.textContent = `${reviving ? "revive" : "switch"} error: ${e}`;
  }
}

async function poll() {
  try {
    const res = await fetch("/api/cascade");
    if (!res.ok) {
      statusEl.textContent = `cascade HTTP ${res.status}`;
      return;
    }
    const trees = await res.json();
    renderTrees(trees);
    if (statusEl.textContent === "loading…") statusEl.textContent = "live";
  } catch (e) {
    statusEl.textContent = `cascade error: ${e}`;
  }
}

async function openConsole() {
  const btn = document.getElementById("console-btn");
  // Deselect any knight; mark the console button active.
  selectedId = null;
  document.querySelectorAll(".row.selected").forEach((r) => r.classList.remove("selected"));
  statusEl.textContent = "opening console…";
  try {
    const res = await fetch("/api/console", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      const err = body.error || body.detail || `HTTP ${res.status}`;
      statusEl.textContent = `console failed: ${err}`;
      return;
    }
    if (btn) btn.classList.add("active");
    statusEl.textContent = `showing ${body.target} (your shell)`;
  } catch (e) {
    statusEl.textContent = `console error: ${e}`;
  }
}

// Switching to a knight clears the console-active state.
function clearConsoleActive() {
  const btn = document.getElementById("console-btn");
  if (btn) btn.classList.remove("active");
}

// Point the embedded terminal iframe at the URL the backend computed from
// JTW_PUBLIC_HOST. Done once at load — the iframe is NEVER reloaded afterwards;
// tmux switch-client changes what it shows underneath.
async function initTerminal() {
  const term = document.getElementById("term");
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cfg = await res.json();
    term.src = cfg.terminal_url;
    if (cfg.auth_required) {
      // The browser itself handles the basic-auth prompt on the ttyd origin;
      // just hint the user if the terminal looks blank until they log in.
      statusEl.title = "terminal requires login (basic-auth)";
    }
  } catch (e) {
    statusEl.textContent = `terminal config error: ${e}`;
  }
}

initTerminal();
document.getElementById("console-btn").addEventListener("click", openConsole);
poll();
setInterval(poll, POLL_MS);
