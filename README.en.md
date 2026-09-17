# Sharp

**A task-execution system for authorized penetration testing.** It models an engagement as an
evidence→action graph, lets an AI worker inside a container drive the work, gates high-risk actions
behind human approval, and keeps everything recorded, queryable, and resumable.

> ⚠️ Use only against systems you have **explicit written authorization** to test. Unauthorized
> testing, exploitation, or data access may be illegal. You are responsible for your actions.
> See `SECURITY.md` for the threat model and operator hardening checklist.

## What it is

Sharp is not a scanner and not a general agent framework. It solves the *engineering* problems of
**long-running** authorized testing:

- **State outlives the process.** Conclusions are written into a graph (SQLite), so restarts and
  container rebuilds do not wipe progress — unlike "conversation history as state", which starts
  over on every crash.
- **Conclusions are traceable.** Every piece of evidence records which action produced it and which
  evidence it rests on, so "why is this finding confirmed?" is a query, not an interview.
- **Boundaries do not depend on the model behaving.** High-risk actions pass a human approval gate
  that sits on the graph write path — you cannot bypass it by driving the API instead of the UI.
- **Assets accumulate.** The endpoint ledger for a host / mini-program / APK is shared across
  engagements, so the second run starts from "what was tested, what is left".
- **Artifacts are classified.** Findings and other structured discoveries share one view. **Scored
  mode (CTF / benchmark) is opt-in** (`task_mode: scored`) and only then do flags and scoring exist —
  a pentest project never shows a "score".

## Core concepts

| Concept | In one line | Internal id |
|---|---|---|
| **Evidence** | Confirmed facts: targets, endpoints, credentials, verified conclusions | `facts` |
| **Action** | One exploration step: start from evidence, test a hypothesis, produce new evidence | `intents` |
| **Hint** | Direction supplied by the human operator | `hints` |
| **Phase goal** | Mid-level milestone that cuts a long task into settleable stages | `sub_goals` |
| **Artifact** | Vulnerabilities / flags / other findings | `vulnerabilities` |
| **Asset** | The tested object and its endpoint ledger | `asset_endpoints` |

Full glossary and naming conventions: `docs/GLOSSARY.md`. Why the graph: `docs/adr/0003-evidence-action-graph.md`.
(These documents are currently Chinese; English translations are tracked as a follow-up — see
`docs/OPENSOURCE_RELEASE.md`.)

## Quick start (macOS / Linux)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker Desktop (for the worker
container), and a model API token.

```bash
# 1) Configure model credentials (Claude / OpenAI / Pi — one or more)
cp secrets.env.example datas/sharp/secrets.env
$EDITOR datas/sharp/secrets.env        # set SHARP_ANTHROPIC_AUTH_TOKEN etc.

# 2) Build the worker image (once, a few minutes, needs network)
cd container && ./fetch_vendor.sh && docker build -t sharp-worker:latest . && cd ..
# On Apple Silicon build natively to skip qemu emulation (much faster scanners in-container):
#   cd container && ARCH=arm64 ./fetch_vendor.sh && docker build --platform linux/arm64 -t sharp-worker:latest . && cd ..
# Note: upstream publishes no linux/arm64 build of naabu, so the arm64 image omits it
# (the Dockerfile skips it automatically); ripgrep falls back to the apt package.

# 3) Start (first run initializes the admin password)
./sharp
# Open http://127.0.0.1:8000 , or use the desktop shell: ./sharp app
```

Diagnostics: `./sharp doctor`. All commands: `./sharp --help`.

## Layout

| Path | Contents |
|---|---|
| `runtime/src/sharp/server` | API server: projects / actions / evidence / approvals / artifacts / asset ledger / knowledge base |
| `runtime/src/sharp/dispatcher` | Scheduler: task lifecycle, worker drivers, prompts |
| `runtime/src/sharp/server/static` | Single-page frontend (no build step: Alpine + Tailwind + Cytoscape) |
| `container/` | Worker image (Dockerfile + `fetch_vendor.sh` + AGENTS) |
| `runtime/tests` | pytest suite (green without a Docker daemon) |
| `docs/` | CHANGELOG / ARCHITECTURE / USAGE / GLOSSARY (updated with every change) |
| `docs/adr/` | Architecture decision records (what was chosen, what was given up) |
| `scripts/` | `check_docs.py`, `check_methods.py`, packaging scripts |
| `tools/` | Maintenance utilities (knowledge re-key, worker trace export, license issuance\*) |

\* `packaging/` and `tools/issue_license.py` build and license the **separately licensed desktop
distribution**; they are not part of the open-source package. See `docs/OPENSOURCE_RELEASE.md`.

## Docs

**Canonical documents are Chinese.** English summaries exist for the two most important
ones; they are written to be useful, not exhaustive, and the Chinese originals win on any
disagreement.

- `docs/USAGE.md` — operator manual (project management / action approval / asset center / mini-program and APK analysis / MCP)
- `docs/USAGE.en.md` — **English summary** of the operator manual
- `docs/ARCHITECTURE.md` — architecture and mechanism contracts (including security and credential conventions)
- `docs/ARCHITECTURE.en.md` — **English summary** of the architecture
- `docs/GLOSSARY.md` — glossary (public term ↔ internal id)
- `docs/CHANGELOG.md` — change log (newest first)
- `docs/adr/` — architecture decision records

## Tests

```bash
cd runtime
uv sync --locked
uv run --with pytest --with httpx pytest        # green without a docker daemon
python3 ../scripts/check_methods.py              # frontend Alpine method completeness
python3 ../scripts/check_docs.py                 # docs ↔ code consistency (numbering, §refs, version claim)
```

## License

AGPL-3.0 (`LICENSE`). Third-party components and their licenses: `THIRD_PARTY_NOTICES.md`.
