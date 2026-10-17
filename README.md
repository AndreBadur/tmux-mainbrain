# tmux-mainbrain

A subagent orchestrator that drives AI knights (kiro-cli sessions in tmux) and
visualizes the running cascade in a small **web app**: a left sidebar tree of
the live knights plus one embedded terminal you can point at any of them.

This README gets you from a fresh clone to the running web app in about a
minute. For the orchestrator internals see [`motor/README.md`](motor/README.md);
for the web app's architecture, routes, and config see
[`journey-tui-web/README.md`](journey-tui-web/README.md).

---

## Quick start (web app) — ~1 minute

### 1. Prerequisites

The installer checks these and fails loud if any is missing:

- **Python 3.11+** (`python3`, with the `venv` module)
- **tmux** on your `PATH`
- **curl** (used once to fetch the `ttyd` terminal binary)
- Linux with **systemd user services** (the app runs as a `--user` service)

On a Debian/Ubuntu host:

```bash
sudo apt-get install -y python3 python3-venv tmux curl
```

### 2. Clone

```bash
git clone git@git-per:AndreBadur/tmux-mainbrain.git
cd tmux-mainbrain
```

### 3. Install + start

One self-contained, idempotent command. It creates a Python venv, installs the
pinned dependencies, downloads `ttyd` into `~/.local/bin`, and registers +
starts a **localhost-only** systemd user service on port `8787`:

```bash
bash install.sh
```

### 4. Open it

```
http://127.0.0.1:8787/
```

That's it. The app auto-creates a console shell, so it works immediately even
before you have launched any knights.

---

## Understand the Realm — just ask the King

You do not need to read the whole design to understand this project. The fastest
onboarding is to **start a King and ask it** — the King is built to explain
itself and the system in plain terms (and will drop the metaphor if you prefer).

Start a King session:

```bash
python3 -m motor spawn wise-king --tmux my-king
tmux attach -t my-king      # then type your questions at the prompt
```

Then ask it, in plain language, questions like:

- **"How do I start?"** — what to run, where to look first.
- **"Who is the King, and why a King?"** — the governing/coordinating role.
- **"Why delegate instead of doing everything yourself?"** — context economy.
- **"Why use the Steward?"** — separation of concerns (forge vs command).
- **"What are you, really — your actual capabilities?"** — the plain-engineering answer.
- **"Why are you 'wise'?"** and **"how are you Socratic?"** — it will *show*, not just define.
- **"How do you delegate, mechanically?"** — spawn → task → `FINAL VERDICT` → collect.
- **"Can you explain this without the medieval metaphor?"** — yes; it will map every role
  to plain terms (orchestrator / agent-factory / research worker), and can use a different
  framing on request.

> Prefer to watch it work? Open the web app (above) and click the King in the
> sidebar to attach the terminal to its session.

---

## Managing the service

```bash
# live logs
journalctl --user -u journey-tui-web.service -f

# restart (e.g. after pulling updates)
systemctl --user restart journey-tui-web.service

# stop + remove the service (keeps your venv and data by default)
bash uninstall.sh
```

`uninstall.sh --purge-venv` also removes the venv; `--purge-data` removes
`journeys/` (your orchestration state) after a typed confirmation.

---

## Security (read before changing binds)

The app and its terminal bind **`127.0.0.1` only** — nothing is exposed to the
network by default. The embedded terminal is a fully interactive shell into the
knights' sessions, so keep it on localhost.

- To reach it from another machine, prefer an SSH tunnel (no binds changed):
  ```bash
  ssh -L 8787:localhost:8787 -L 7681:localhost:7681 <user>@<this-host>
  ```
  then browse `http://localhost:8787/` locally.
- Only bind beyond localhost with a credential set
  (`JTW_TTYD_BIND=0.0.0.0` **and** `JTW_TTYD_CRED=user:pass`). See
  [`journey-tui-web/README.md`](journey-tui-web/README.md#security) for the full
  guidance and the env-var reference.

---

## Running the app manually (development)

If you want it in the foreground instead of as a service:

```bash
cd journey-tui-web
PYTHONPATH="$PWD/.." TMUX_TMPDIR=/tmp PATH="$HOME/.local/bin:$PATH" \
  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8787
```

(The venv is created by `install.sh`; run it once first.)

---

## Repository layout

| path | what it is |
|------|------------|
| `install.sh` / `uninstall.sh` | one-shot web-app installer / remover |
| `journey-tui-web/` | the FastAPI web app (sidebar cascade + ttyd terminal) |
| `motor/` | the deterministic engine that drives tmux/kiro sessions |
| `.kiro/` | agent definitions and steering rules for the knights |
| `journeys/` | per-journey operational state (git-ignored) |
