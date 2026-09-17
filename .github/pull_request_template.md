## What changed

<!-- One paragraph. If this fixes a defect, describe the failure mode (how it was found, what it
did wrong), not just the patch. -->

## Why this shape

<!-- Alternatives you rejected and why. -->

## Verification

- [ ] `uv run --project runtime --with pytest --with httpx pytest -q` passes
- [ ] `python3 scripts/check_methods.py` passes (if frontend touched)
- [ ] `python3 scripts/check_docs.py` passes
- [ ] New guards are **mutation-tested**: I removed the wiring and confirmed the test fails
      (state which test and what it reported)

## Docs

- [ ] `docs/CHANGELOG.md` (newest entry on top)
- [ ] `docs/ARCHITECTURE.md` (if a mechanism changed)
- [ ] `docs/USAGE.md` (if operator-visible behaviour changed)
- [ ] `README.md` / `README.en.md` (if setup or layout changed)

## Does it write shared state?

<!-- Any path that lands in the knowledge base, asset ledger, env baseline, or evidence graph. -->

- [ ] No
- [ ] Yes → guarded against false entries (explain how; a wrong `dead_end` or a truncated credential
      is worse than a missing one)

## Not done

<!-- Explicitly out of scope, with the reason. Keeps reviewers from guessing. -->
