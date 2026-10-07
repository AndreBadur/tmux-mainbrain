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

// --- mobile drawer -------------------------------------------------------
// The sidebar is a slide-in drawer on narrow screens. We toggle a class AND set
// the transform inline as a belt-and-suspenders guard against any CSS cascade
// surprise (specificity / order), so the hamburger always works.
function isMobile() {
  return window.matchMedia("(max-width: 768px), (max-height: 500px)").matches;
}
function openDrawer() {
  const sb = document.getElementById("sidebar");
  sb.classList.add("open");
  sb.style.transform = "translateX(0)";
  document.body.classList.add("drawer-open");
  const bd = document.getElementById("backdrop");
  if (bd) bd.style.display = "block";
  const t = document.getElementById("menu-toggle");
  if (t) t.setAttribute("aria-expanded", "true");
}
function closeDrawer() {
  const sb = document.getElementById("sidebar");
  sb.classList.remove("open");
  // Only re-hide off-screen on mobile; on desktop leave the sidebar visible.
  sb.style.transform = isMobile() ? "translateX(-100%)" : "";
  document.body.classList.remove("drawer-open");
  const bd = document.getElementById("backdrop");
  if (bd) bd.style.display = "none";
  const t = document.getElementById("menu-toggle");
  if (t) t.setAttribute("aria-expanded", "false");
}
function toggleDrawer() {
  const sb = document.getElementById("sidebar");
  if (sb.classList.contains("open")) closeDrawer();
  else openDrawer();
}
// Scroll the embedded kiro-cli TERMINAL's tmux scrollback via the backend
// (POST /api/scroll/{up|down}). The terminal is a cross-origin iframe the page
// cannot scroll directly, so we drive tmux copy-mode server-side instead.
async function scrollTerminal(direction) {
  try {
    const res = await fetch(`/api/scroll/${direction}`, { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      statusEl.textContent = `scroll failed: ${body.error || body.detail || res.status}`;
    }
  } catch (e) {
    statusEl.textContent = `scroll error: ${e}`;
  }
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
    closeDrawer();  // on mobile, return to the terminal after switching
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
    closeDrawer();  // on mobile, return to the terminal after opening console
  } catch (e) {
    statusEl.textContent = `console error: ${e}`;
  }
}

// Switching to a knight clears the console-active state.
function clearConsoleActive() {
  const btn = document.getElementById("console-btn");
  if (btn) btn.classList.remove("active");
}

// --- New Journey: show inline form, POST /api/journey, refresh on success ----
function showNewJourneyForm(show) {
  const form = document.getElementById("newjourney-form");
  const btn = document.getElementById("newjourney-btn");
  const err = document.getElementById("newjourney-err");
  if (!form || !btn) return;
  form.hidden = !show;
  btn.hidden = show;
  if (err) err.textContent = "";
  if (show) {
    const inp = document.getElementById("newjourney-name");
    if (inp) { inp.value = ""; inp.focus(); }
  }
}

async function submitNewJourney(ev) {
  if (ev) ev.preventDefault();
  const inp = document.getElementById("newjourney-name");
  const err = document.getElementById("newjourney-err");
  const createBtn = document.getElementById("newjourney-create");
  const name = (inp && inp.value || "").trim();
  if (!name) { if (err) err.textContent = "name required"; return; }
  if (err) err.textContent = "";
  if (createBtn) createBtn.disabled = true;
  statusEl.textContent = `creating journey "${name}"… (spawning wise-king)`;
  try {
    const res = await fetch("/api/journey", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) {
      const msg = body.error || body.detail || `HTTP ${res.status}`;
      if (err) err.textContent = msg;
      statusEl.textContent = `new journey failed: ${msg}`;
      return;
    }
    statusEl.textContent = `journey "${body.journey_id}" created (king ${body.king_session})`;
    showNewJourneyForm(false);
    poll();  // reflect the new L1 king in the cascade immediately
  } catch (e) {
    if (err) err.textContent = String(e);
    statusEl.textContent = `new journey error: ${e}`;
  } finally {
    if (createBtn) createBtn.disabled = false;
  }
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

// Wire everything after the DOM is ready, guarding each lookup so a single
// missing element can never abort the rest of the wiring.
function bind(id, event, handler) {
  const node = document.getElementById(id);
  if (node) node.addEventListener(event, handler);
  else console.warn(`bind: #${id} not found`);
}

function init() {
  initTerminal();
  bind("console-btn", "click", openConsole);
  bind("menu-toggle", "click", toggleDrawer);
  bind("backdrop", "click", closeDrawer);
  bind("scroll-up", "click", () => scrollTerminal("up"));
  bind("scroll-down", "click", () => scrollTerminal("down"));
  bind("newjourney-btn", "click", () => showNewJourneyForm(true));
  bind("newjourney-cancel", "click", () => showNewJourneyForm(false));
  bind("newjourney-form", "submit", submitNewJourney);
  poll();
  setInterval(poll, POLL_MS);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
