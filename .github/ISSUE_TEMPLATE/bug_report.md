---
name: Bug report
about: Something behaves incorrectly or a mechanism does not take effect
title: "[bug] "
labels: bug
---

**What happened**

**What you expected**

**How to reproduce**
1.
2.

**Which surface** (tick all that apply)
- [ ] web UI
- [ ] API (`/projects`, `/dispatcher/...`)
- [ ] dispatcher / task lifecycle
- [ ] worker container / provider driver
- [ ] knowledge base or asset ledger
- [ ] docs

**Environment**
- Sharp version / commit:
- OS + Python + Docker version:
- Worker driver (`claudecode` / `codex` / other) and model:
- `GET /health` output (it reports `code_rev` / `disk_rev` / `stale` — please include it):

**Evidence**
- Relevant log lines (`/tmp/sharp_serve.log`, `/tmp/sharp_dispatch.log`), DB rows, or a trace from
  `tools/export_worker_trace.py`.

**Does it write wrong state?**
If the bug causes a false conclusion, a fake `dead_end`, or a truncated credential to be stored,
please say so explicitly — wrong shared state is treated as high severity here, because the planner
treats it as established fact.
