# Sharp — Operator Manual (English summary)

> **This is a summary, not the canonical document.** `docs/USAGE.md` (Chinese) is the source of
> truth and covers every screen in detail; section numbers below match it. If the two disagree, the
> Chinese document wins — please open an issue so the summary gets fixed.

## 1. What Sharp is

A task-execution system for **authorized penetration testing**. You define a target and a goal; an AI
worker inside a container works through it; you keep the high-risk decisions. Everything it concludes
becomes auditable evidence, and the project survives restarts.

Not a scanner (it does not run a fixed checklist) and not a general agent framework (it exists to
produce defensible conclusions about one engagement).

> ⚠️ Authorized targets only. See `SECURITY.md` for the threat model and hardening checklist.

## 2. Requirements

Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (for worker containers), and a model provider
credential. macOS and Linux are the primary platforms.

## 3. Install and configure

**Worker image** — one build, a few minutes, needs network:

```bash
cd container && ./fetch_vendor.sh && docker build -t sharp-worker:latest . && cd ..
# Apple Silicon: build natively (much faster scanners in-container, no qemu)
#   cd container && ARCH=arm64 ./fetch_vendor.sh && docker build --platform linux/arm64 -t sharp-worker:latest . && cd ..
```

Upstream publishes no `linux/arm64` build of naabu, so the arm64 image omits it (the Dockerfile skips
it automatically).

**Model credentials** — `cp secrets.env.example datas/sharp/secrets.env` and fill it in. The file is
git-ignored and must never be committed or zipped. It supports Claude / OpenAI / Pi.

**Model and endpoint** (`dispatch.yaml`, advanced) — defaults point at the **official** Anthropic
endpoint with the version-agnostic `sonnet` alias. Override per environment:

```yaml
      ANTHROPIC_MODEL: "${SHARP_MODEL:-sonnet}"
      ANTHROPIC_BASE_URL: "${SHARP_BASE_URL:-https://api.anthropic.com}"
```

`${VAR:-default}` is resolved from the process environment, and `secrets.env` is loaded into it, so
values set there win over the shipped defaults. Do not bake a third-party gateway into the default —
everyone who lacks an account there would fail on first run.

**First start** — `./sharp` runs `uv sync`, checks keys, and initialises the admin password.
`./sharp doctor` diagnoses the environment.

## 4. How to start it

```bash
./sharp            # server + dispatcher, browser at http://127.0.0.1:8000
./sharp app        # desktop window shell
./sharp doctor     # environment diagnosis
```

The server binds to `127.0.0.1`; do not expose it to a network you do not control.

## 5. Users and authentication

First run initialises an admin password. Sessions use a JWT whose signing secret is random per
process — restart the server and you log in again. The browser keeps the token in `sessionStorage`
(gone when the tab closes). There is a mandatory per-session authorization-terms confirmation: Sharp
is only for systems you are authorized to test.

## 6. Projects

A project is one engagement: **origin** (target + what you already know) and **goal** (what must be
verified or obtained). Templates exist for the common shapes. Projects have a state
(initial / active / stopped / completed), an optional deadline, and a task budget.

- **Target kinds**: `web`, `miniprogram`, `android` — the last two start the corresponding analyzer.
- **Asset key** (`asset_ref`): the host / AppID / package name that ties this project to the asset
  ledger and to knowledge from previous engagements.
- **Task mode**: `pentest` (default) or `scored` (CTF / benchmark). Only `scored` shows flags and
  points.
- **Asset center**: one row per asset, aggregating projects, findings, unverified endpoints, and —
  clicking through to the space detail — the whole history of that asset across projects.

> **Goal text is the most load-bearing field you will write.** If the platform or the client gives
> you a task brief (technology in scope, expected last step, where the secrets live), put it in the
> goal verbatim. A worker that never sees the brief has to rediscover the environment from zero —
> which looks like "the tool is stuck" but is actually a setup omission.

## 7. The evidence–action graph

The graph is the project's state: evidence as nodes, actions as edges, hints attached as direction.

- **Operations**: drag to lay out, click to inspect, multi-select facts to create an action, four
  layout engines, lineage highlighting.
- **Side panel tabs**: detail (with action buttons), **hints** (operator direction), log (event
  timeline), plan (**phase goals + open hypotheses**), live (worker output).
- **Workbench views**: graph / action board / evidence chain.
- **Human correction**: click any non-origin fact to correct or distrust it. Corrections are
  audited, and a human rewrite counts as a review — the corrected text becomes trusted.
- **Acceptance check** before completing a project: open actions, pending approvals, untrusted
  evidence, unconfirmed high-severity items, unfinished phases. Informational, not blocking.

**Hints are the steering channel.** They are re-read by the planner on every cycle, so you can add
one mid-engagement ("this dead end is closed, go here instead") and it takes effect the same cycle.
Hints outrank the graph when they contradict it — you can see things the agent cannot — but a hint is
a direction, not evidence.

## 8. Actions (intents)

An action is one exploration step. Lifecycle: created → claimed by a worker → executing → concluded
with evidence, or abandoned with a reason. The panel shows a heartbeat progress bar for what is
running.

- **Approval gate**: high/critical-risk actions wait for a human decision **before** execution; the
  agent cannot self-approve. This is what stops "the AI decided to try something destructive".
- **Emergency mode**: a time-boxed bypass of the *confirmation*, never of the *record*. Every
  auto-released action becomes an `emergency_release` event, listed in the approval center's
  "emergency releases" tab and reviewable afterwards (ok / follow up + note). Reviewing is the
  control that replaces approving.
- **Budget and pause**: cap how many tasks a project may run, and pause scheduling without killing
  running tasks (useful for a graceful restart).
- **Knowledge reuse**: this target's and this product's accumulated knowledge is injected into every
  planning cycle. Read it critically when you see `dead_end` entries — the planner treats them as
  "do not retry", so a wrong one costs real time. Sharp refuses to store obviously synthetic
  credentials for the same reason.
- **Dual-account IDOR testing**: the worker is asked to establish a second identity before claiming
  horizontal-access findings, and findings must name which identity produced which differential.
- **MCP tools**: external MCP servers can be configured for the worker.
- **AI behaviour mechanisms**: hypotheses (falsifiable, settled with evidence), environment baseline
  (confirmed preconditions, never re-probed), deliverability (a conclusion without evidence is
  flagged, not shipped), and, in scored mode, flag normalization.

## 9. Timeline and replay

The timeline is the project event stream; replay reconstructs the sequence of facts and actions so
you can explain afterwards how a conclusion was reached.

## 10–11. APK and mini-program analysis

Both have dedicated analyzers: shallow analysis (permissions, SDKs, endpoints, hardcoded secrets),
an AI conversation over the artifact, unpacking / decompilation, dynamic analysis (device hooking for
APK, HAR capture for mini-programs), and "dispatch to project" to turn findings into a project's
starting evidence.

## 12. General AI chat

A project-scoped chat for reasoning about the engagement. It can write evidence into the graph, which
then flows through the same hooks as worker conclusions (knowledge extraction, endpoint ledger).

## 13. Reports and export

- **Project export** — the full graph as YAML.
- **AI report generation** — a narrative report from confirmed evidence (long-running; the request
  timeout is widened for this path).
- **Coverage report** — what was actually covered: what was hit, what was proven not to work, the
  blind spots (discovered-but-never-verified endpoints, abandoned actions with reasons, untrusted
  evidence), and what it cost. It deliberately does **not** claim completeness.
- **Handover** — reports and the asset space detail are designed to hand an engagement to someone
  else without re-reading the whole graph.

## 14. Settings

Local preferences (browser side), server settings, secrets configuration, and dispatcher status.
Changing `dispatch.yaml` requires restarting the dispatcher — and note `/health` reports
`code_rev` / `disk_rev` / `stale` precisely so you can tell "the process is up" from "the process is
running the code you just edited".

## 15. Keyboard shortcuts

Listed in the UI (`docs/USAGE.md §15` for the full table).

## 16. FAQ (short answers)

| Question | Answer |
|---|---|
| Requests spinning / timing out | Front-end calls have budgets; long endpoints are widened per path. Check the server log — if the DB is locked by another process, restart cleanly. |
| Why confirm the authorization terms every session? | Because the tool is dual-use; it is deliberately in the path, not a nuisance dialog. |
| Why log in again after restarting? | The JWT signing secret is random per process by design. |
| Project stuck in the initial state | Bootstrap produces nothing when a heavy scan eats its whole budget. Reduce scan volume in the goal or raise the bootstrap timeout. |
| Worker container fails to start | Check Docker is running and the image exists (`docker images | grep sharp-worker`); `./sharp doctor` reports what it found. |
| Forgot the password | Reset it from the CLI (see `docs/USAGE.md §16`). |
| `dispatch.yaml` changes have no effect | The dispatcher reads it at startup — restart the dispatcher. |
| Watching worker output live | The project's "live" tab streams it. |
| Multiple users | Single instance, single machine: one server + one dispatcher, shared via the web UI. |

## 17. Where data lives

| Path | Contents |
|---|---|
| `~/.local/share/sharp/sharp.db` | the database (all projects, evidence, knowledge) — unencrypted, protect it |
| `datas/sharp/secrets.env` | provider credentials — mode `600`, never committed |
| `datas/sharp/*.lock` | single-instance locks; a stale file after a crash must be removed |
| `reports/` | generated reports — engagement data, keep it out of shares |
| `~/.claude/projects/...` (inside the container) | the raw agent session transcripts; export with `tools/export_worker_trace.py` |
