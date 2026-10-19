# Contributing to Sharp

Thanks for considering it. Sharp is a small project with an unusually strict documentation and
verification culture — the rules below are not ceremony, they are how the codebase stays
trustworthy. Read this before your first PR.

## Development setup

```bash
git clone <your-fork> && cd Sharp-v2

# Runtime deps (locked) — this also creates runtime/.venv
uv sync --project runtime --locked

# Backend
uv run --project runtime sharp serve --host 127.0.0.1 --port 8000   # dispatcher needs a worker image
```

The frontend has **no build step**: `index.html` + `static/views/*.html` are assembled by
`<!-- @include views/x.html -->` and served directly. Edit the HTML/JS and refresh — the include
cache is mtime-based, so no rebuild is needed (bump `?v=N` on the script tags when you change a JS
module; CI checks that the version claim and the method inventory agree).

Running the dispatcher requires a worker image (`container/`, a few minutes to build) and a model
provider credential. See the README quickstart.

## The three checks every PR must pass

```bash
# Canonical test command (pytest/httpx are pulled ephemerally on purpose —
# they must never be locked into the runtime venv)
uv run --project runtime --with pytest --with httpx pytest -q

python3 scripts/check_methods.py    # frontend: every Alpine method referenced must exist
python3 scripts/check_docs.py       # docs↔code: numbering, §refs, module-tree claims, version claim
```

All three run in CI (`.github/workflows/tests.yml`). They are not advisory.

## House rules that reviewers will hold you to

1. **A change lands with its documentation.** `docs/CHANGELOG.md` (newest entry on top),
   `docs/ARCHITECTURE.md` (mechanism), `docs/USAGE.md` (operator-facing behaviour). If a mechanism
   changes and the docs do not, the change is incomplete — and `check_docs.py` may catch part of it.
2. **Record the *why*, not just the *what*.** The valuable part of a CHANGELOG entry here is the
   failure mode: what was broken, how you found it, and why the fix is the right shape. "Refactor
   X" tells the next reader nothing.
3. **Prefer structured signals over prose inference.** Do not derive state from keywords in
   free-text conclusions (past incident: a phase estimator read "不存在未认证 SSRF" as evidence
   *for* SSRF, and steered the planner the wrong way). Count rows in tables instead.
4. **Guard the wiring, not just the unit.** The dominant defect class in this project is *a
   mechanism that was built but has no consumer* — a prompt placeholder nobody fills, a field
   nobody reads, a channel with three producers and zero consumers. When you add a block to a
   prompt, add a test that asserts the placeholder is actually fed (see
   `runtime/tests/test_operator_hints.py` for the pattern). `render_prompt` is a bare `str.replace`:
   a missing key fails **silently** by leaving `{placeholder}` in the prompt.
5. **Mutation-test your new guards.** Delete the wiring you just added and confirm your test fails.
   A guard that passes both with and without the fix is not a guard. Say so in the PR description.
6. **Never write a false positive into shared state.** A wrong `dead_end` or a truncated credential
   in the knowledge base is worse than a missing one: the planner treats it as established fact.
   Skip what you cannot confirm.

## Adding a worker or a driver

Workers live in `runtime/src/sharp/dispatcher/workers/` and are declared in `dispatch.yaml`
(`type` selects the driver implementation). A new worker must implement the driver interface
(healthcheck / execute / conclude command builders) and be covered by a test that asserts the
commands it builds. Provider-specific defaults belong in `dispatch.yaml`, not in code.

## Adding a knowledge kind or a table

Anything the worker can conclude and the reason loop consumes is part of the protocol. Add it to:
the contracts (`dispatcher/contracts.py`) with a validator, the prompt output examples, the
server-side writer, and a test that a payload using it survives validation end-to-end. The
validator and the prompt must agree — a mismatch silently rejects the worker's whole conclusion
(this happened: 1750 characters of findings were dropped because the prompt advertised a field the
validator did not allow).

## Documentation language

The **Chinese documents are canonical** (`docs/ARCHITECTURE.md`, `docs/USAGE.md`, `CHANGELOG.md`).
`docs/ARCHITECTURE.en.md` and `docs/USAGE.en.md` are English **summaries**: write them to be
useful rather than exhaustive, keep section numbers aligned with the Chinese originals, and if
you change a mechanism, update the Chinese document first and then either update the summary or
open an issue saying the summary is behind. A summary that has silently drifted is worse than no
summary — state the canonical pointer at the top and keep it honest.

## Commit and PR style

- Small, single-purpose commits; imperative subject lines.
- PR description: what changed, why, how you verified it, and what you explicitly did **not** do.
- If you ran an end-to-end check against a live target, say what it proved — live evidence beats
  test counts.
- Do not commit: `datas/sharp/secrets.env`, `datas/sharp/*.db*`, `datas/sharp/license_signing_key.b64`,
  `license.key`, anything under `reports/`, or any downloaded `container/vendor/` binary.
  `.gitignore` covers these — verify with `git status` before you push.

## Scope of contributions

The core (server, dispatcher, protocol, docs) is AGPL-3.0. Packaging for the licensed desktop
distribution (`packaging/`, license issuance) is maintained separately and is out of scope for
community PRs — see `docs/OPENSOURCE_RELEASE.md` for the boundary.
