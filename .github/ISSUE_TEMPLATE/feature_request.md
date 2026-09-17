---
name: Feature request
about: Propose a capability, or a change to how Sharp works
title: "[feature] "
labels: enhancement
---

**Problem**
What can you not do today, or what does Sharp get wrong? Describe the situation, not the solution.

**Proposed behaviour**

**How would you verify it?**
What evidence would show this works — a test, a live run against a lab target, a DB query?

**Which layer does it touch?**
- [ ] protocol (evidence/action/hint schema, contracts, validators)
- [ ] planner (reason prompt, phase guidance, hypotheses)
- [ ] executor (worker prompt, drivers, containers)
- [ ] server (tables, API, web UI)
- [ ] docs only

**Scope check**
- Does it change what gets written into shared state (knowledge base, asset ledger, env baseline)?
  If yes, describe the failure mode you are guarding against — a wrong entry outlives the run and
  is treated as fact.
- Does it need to hold for the whole project life, or only until the next restart?
