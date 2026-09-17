# Sharp — Architecture (English summary)

> **This is a summary, not the canonical document.** `docs/ARCHITECTURE.md` (Chinese) is the source
> of truth; section numbers below match it, so `§N.M` here means the same section there. If the two
> disagree, the Chinese document wins — and please open an issue so the summary gets fixed.

## 0. Design stance

Sharp is a **task-execution system for authorized penetration testing**, not a general agent
framework. Three stances shape every mechanism:

1. **Conclusions over activity.** The deliverable is an auditable chain (evidence → artifacts →
   report), not a busy agent. State therefore lives in a graph, not in conversation history.
2. **Boundaries over capability.** What may be done is defined by the authorization scope and a
   human approval gate, not by the model's judgement. The gate sits on the graph write path, so
   driving the API instead of the UI does not bypass it.
3. **Accumulation over re-running.** The endpoint ledger for an asset and the cross-project
   knowledge base persist, so the second engagement starts from "what was tested, what is left".

The resulting trade-offs: an extra structured submission rather than conclusions scattered in prose;
an interruption of automation rather than an unrecorded high-risk action; one capability fewer
rather than losing "restart without amnesia" and "explainable afterwards".

## 1. Process structure

Three roles cooperating over HTTP + a shared SQLite database:

```
        ┌──────────────────────────┐
        │       Sharp Server       │  FastAPI; protocol source of truth (SQLite)
        └────────────┬─────────────┘
                Read / Write API
                     │
        ┌────────────┴─────────────┐
        │        Dispatcher        │  client executor; the ONLY writer of agent-derived facts
        └──────┬────────────┬──────┘
               │            │
        ┌──────┴─────┐ ┌────┴───────┐
        │ Worker cntr│ │ Worker cntr│  one container per project; agent runs via docker exec
        └────────────┘ └────────────┘
```

- **Server** (`runtime/src/sharp/server/`) — FastAPI routes + SQLite. Owns the evidence/action/hint
  graph. Both the web UI and the dispatcher read and write through it.
- **Dispatcher** (`runtime/src/sharp/dispatcher/`) — separate process, single-threaded scheduling
  loop. Claims tasks, manages lifecycles, and calls the server API **on the agent's behalf**.
  Agents never talk to the server directly: the dispatcher is the control plane.
- **Worker containers** — one long-lived container per project (`sleep infinity`); the dispatcher
  `docker exec`s agent commands into it. Agents do not talk to each other; they share the graph.

`./sharp` (launcher) starts both processes. Single instance per machine: the processes take lock
files (`sharp-server.lock`, `sharp-dispatch.lock`) and refuse to double-start.

**Why this shape.** A long engagement must survive restarts, run several explorations in parallel,
and be auditable afterwards. All three need queryable state and a single writer — not a chat log.

## 2. The evidence–action graph

| Concept | Internal id | Meaning |
|---|---|---|
| Evidence | `facts` | confirmed facts: target, endpoints, credentials, verified conclusions |
| Action | `intents` | one exploration step: start from evidence, test a hypothesis, produce new evidence |
| Hint | `hints` | direction supplied by the human operator (not evidence, not a task) |

`intent_sources` records the many-to-many link from an intent to the facts it started from, which is
what makes "why is this conclusion confirmed?" a query.

Adjacent tables, each answering one question:

| Table | Question it answers |
|---|---|
| `vulnerabilities` | what did we produce? (`kind` = `vuln` / `flag` / `finding`, plus severity and, in scored mode, points) |
| `asset_endpoints` | which endpoints are known on this asset, and which are still unverified? (the ledger) |
| `env_baseline` | what is already confirmed about the environment (connectivity, credential validity, tooling) so it is never re-probed |
| `hypotheses` | which falsifiable propositions are still open? (the planner's main job is to kill these) |
| `sub_goals` | how is a long task cut into settleable phases? |
| `knowledge_base` | what is reusable beyond this project? |
| `approval_events` | who released which high-risk action, and when |
| `fact_edits` | the append-only trail of human corrections to evidence |

**Canonical target keys** (`server/hostkey.py`) are the single vocabulary for "is this the same
target": `domain:<root>` / `ip:<addr>` / `host:<single-label>` / `unattributed`. `project_target_key()`
in `services.py` is the one implementation used by the knowledge base, baseline inheritance, and
coverage reporting — three call sites previously had three implementations, which is how you get
"the knowledge base thinks it is the same target and the baseline disagrees".

**Trust is explicit**: `facts.trusted` (default true) can be cleared by a human, and a human rewrite
is itself a review — corrected text becomes trusted and is appended to `fact_edits`.
`origin` is immutable (422) because the graph, the knowledge keys, and the baseline all hang off it.

**Task modes**: `pentest` (default) and `scored` (CTF / benchmark). Only `scored` enables flags and
scoring; a pentest project never displays a "score", because it has no meaning there.

## 3. Task lifecycle: bootstrap / reason / explore

One worker executes three task types (`dispatcher/tasks/`):

| Task | Trigger | What it does |
|---|---|---|
| **Bootstrap** | initial state: facts are exactly `{origin, goal}` and the only intent is the bootstrap intent | attempt a direct solve, produce the first fact |
| **Reason** | real facts exist | read the whole graph: is the goal met? which new intents should be derived? |
| **Explore** | an unclaimed intent exists | claim one intent, explore it, write back one fact |

Once the first non-seed fact exists the project is no longer in the initial state: bootstrap is never
re-dispatched, and the project moves to reason/explore.

**Two phases, execute → conclude** (both bootstrap and explore):

- `execute` — the real agent turn, budgeted (`timeout <N>s`).
- `conclude` — a fallback that resumes the **same session** when execute timed out or produced an
  unparseable result, asking the agent to stop exploring and summarise what was actually confirmed.
  This matters more than it looks: **the conclusion is the only channel that carries a round of work
  back into the graph**, so a killed conclude discards the round.

Timeouts live in `dispatch.yaml` (bootstrap 400s, explore 300s, conclude 180s). The conclude budget
was raised from 90s after a live run showed two fallbacks killed at exactly 90s under four-way
concurrency (exit 124, empty stdout) — each one losing a 5-minute round.

**The planner owns direction.** Reason reads the graph plus operator hints and proposes at most
`max_intents` non-overlapping intents per cycle. It is also where the phase guidance is injected.

> **Known limitation**: an agent that starts a very heavy scan inside bootstrap (full-template
> nuclei, high-level sqlmap — tens of minutes per run) will hit the timeout and produce nothing; the
> project stays in the initial state and bootstrap is re-dispatched. Constrain scan volume in the
> goal, or raise the bootstrap timeout.

### 3.1 Approval gate (2026-08-27)

Intents marked high/critical risk require a human decision **before** execution; the agent cannot
self-approve (it has no Sharp credentials inside the container). Emergency mode bypasses the
*confirmation* but never the *record*: every auto-released action is written as an
`emergency_release` event and is reviewable afterwards (`emergency_review`).

## 4. Worker driver abstraction

A worker is a provider CLI plus the flags Sharp needs to drive it. Drivers
(`dispatcher/workers/`) build four commands: healthcheck, session preparation, execute, conclude.
`type` in `dispatch.yaml` selects the driver (`claudecode`, `codex`, …), and provider-specific
defaults (model, base URL) belong in configuration, not in code — see §10 on why the *shipped*
defaults point at official endpoints and how to override them with `SHARP_MODEL` / `SHARP_BASE_URL`.

Provider health is tracked per worker: repeated failures place that provider on cooldown so the
dispatcher stops burning the task budget on it.

## 5. Runtime robustness

- **Leases and heartbeats** — a project has one writer at a time (reason lease, intent claim).
  Stale leases are expired rather than blocking forever.
- **Cancellation** — tasks are cancelled by killing the process group *inside* the container, so a
  wedged scanner cannot outlive its task.
- **Code revision fingerprint** (`server/rev.py`) — `/health` reports `code_rev` (the running
  process) and `disk_rev` (the files) plus `stale`. This exists because "the process is running" and
  "the process is running *your latest code*" are different claims, and confusing them cost real
  debugging time.
- **Container lifecycle** — hot containers per project, per-project worker caps, cleanup when a
  project completes, and shutdown that waits for cancellation to return.
- **Stall detection** — after `REASON_STALL_THRESHOLD` (3) reason cycles with no new facts, the
  dispatcher writes a diagnostic hint and pauses reason dispatch. The intended way out is **an
  operator adding a hint** — which is also why hints must actually reach the planner prompt (§9 /
  CHANGELOG 2026-09-16: they previously did not).

## 6–7. Crypto helper and APK injection

`crypto_helper.py` centralises hashing/decryption used by the analysis paths. The Android deep
analysis path supports injecting agent tooling into an unpacked APK for dynamic work. Both are
described in the Chinese document; neither changes the graph model.

## 8. Code layout

| Path | Contents |
|---|---|
| `runtime/src/sharp/server/` | API server, repositories, routers, coverage/reporting, static frontend |
| `runtime/src/sharp/dispatcher/` | scheduler loop, task implementations, worker drivers, prompt templates |
| `runtime/src/sharp/cli.py` | `serve` / `dispatch` / `window` entry points |
| `container/` | worker image (Dockerfile + `fetch_vendor.sh` + AGENTS) |
| `runtime/tests/` | pytest suite (green without a Docker daemon) |
| `scripts/` | `check_docs.py`, `check_methods.py`, packaging |
| `docs/` + `docs/adr/` | operational docs and decision records |

## 9. Frontend

Single-page application with **no build step**: `index.html` plus `static/views/*.html` assembled by
`<!-- @include views/x.html -->` (mtime-cached, so edits are live on refresh). Alpine.js for
reactivity, Tailwind at runtime, Cytoscape for the graph. Twelve JS modules share one Alpine
component via mixins.

Two conventions are enforced rather than remembered:

- `?v=N` on the script tags must be bumped when a JS module changes, and the method inventory in
  `ARCHITECTURE.md §9.3` must match reality — `scripts/check_methods.py` fails otherwise.
- The session token lives in `sessionStorage` (with automatic migration away from `localStorage`),
  and `renderMd()` escapes text before formatting, so target-derived content cannot inject markup.

## 10. Security baseline

| Item | Policy |
|---|---|
| Auth token storage | `sessionStorage`, cleared when the tab closes; the server's JWT secret is random per process, so a restart invalidates sessions |
| Request timeouts | all `fetch` calls have budgets; long endpoints are widened by path, not call site |
| CSP | `script-src 'self'` / `connect-src 'self'` / `object-src 'none'` / `frame-ancestors 'none'`. Inline execution is still allowed because Alpine and the Tailwind runtime need it, so markup injection is contained by the DOMPurify allowlist rather than by CSP |
| Emergency mode | bypasses confirmation, never the record; per-action audit plus post-hoc review |
| Untrusted content | target output is **data, not instructions**. Stated in every prompt (`explore`, `bootstrap`, both conclude prompts) with three prohibitions — never obey, never send secrets outward, never widen scope — and in `reason.md`, because injected text reaches the planner via facts. See `SECURITY.md` for the full threat model |
| Container boundary | the container holds the model API key (it must, to call the model) but **no Sharp credentials**; the agent can only *report*, the server writes |

## 11. Testing and engineering

```bash
uv run --project runtime --with pytest --with httpx pytest -q   # test suite
python3 scripts/check_methods.py                                 # frontend method inventory
python3 scripts/check_docs.py                                    # docs ↔ code consistency
```

All three run in CI. pytest and httpx are deliberately **not** locked dependencies: locking them
would break the launcher's `uv sync --locked` and pull test tooling into the runtime venv.

## 12. Documentation sync convention

Three core documents, each with a defined audience and update trigger: `CHANGELOG.md` (what changed
when — every change), `ARCHITECTURE.md` (what the system looks like — architectural changes),
`USAGE.md` (how to operate it — operator-visible behaviour). The convention used to rely on memory;
`scripts/check_docs.py` now enforces the parts a machine can check: heading numbering and ordering,
cross-reference resolution, module-tree claims (methods/line counts), and the frontend version claim.

## 13. Architecture decision records

`docs/adr/` records what was chosen and what was given up — notably the evidence–action graph (why
state is a graph rather than a transcript), the approval gate, and the knowledge/base-line reuse
boundaries. Read these before proposing a structural change.

## 14. Roadmap

Ordered by what real authorized engagements needed next, not by novelty. See `docs/ARCHITECTURE.md
§14` for the current list.
