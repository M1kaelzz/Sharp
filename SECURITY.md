# Security Policy

Sharp is a task-execution system that lets an AI worker **autonomously run commands** against a
target you are authorized to test. That design makes two things security-relevant: the tool's own
attack surface, and the blast radius of the worker it drives. This document covers both.

## Reporting a vulnerability

- **Preferred**: open a private report through GitHub's [Security Advisories](../../security/advisories/new)
  (Security → Report a vulnerability). That channel is private and gives us a place to coordinate a fix.
- Please do **not** open a public issue for anything exploitable before a fix ships.
- Include: affected version/commit, what you did, what happened, and the impact you believe it has.
- **TODO(maintainer)**: add a security contact address here if you want email reports as well.

We aim to acknowledge within 7 days. This is a small project — no bug bounty, but credit in the
release notes if you want it.

## Threat model

### What we are protecting

| Asset | Where it lives | Why it matters |
|---|---|---|
| Model provider API key | worker container env, and `datas/sharp/secrets.env` on the host | pay-per-token credential; leaks are costly |
| Engagement data (evidence, credentials, reports) | Sharp's SQLite DB, `reports/` | contains the target's secrets and your findings |
| Operator host and network | the machine running Sharp and Docker | the worker runs commands; the container is the only wall |
| The target | reached over the network by the worker | actions must stay inside your authorization |

### Trust boundaries

1. **Target content is adversary-controlled input.** Everything the worker reads from a target —
   HTTP bodies, error messages, file contents, banners, prompt-like text — can be crafted to
   instruct the agent. This is a *prompt injection* surface by design: the agent is meant to read
   hostile input and act. Assume a target can make the agent run arbitrary commands **inside its
   container**, and can make it send requests **anywhere the container can reach**.
2. **The container is the isolation boundary.** One container per project. It holds the model API
   key (the CLI needs it) and has outbound network access (it must reach targets). It does **not**
   hold Sharp API credentials — by design the worker only *reports* conclusions and the server
   writes them, so an injected instruction cannot rewrite your graph directly.
3. **The Sharp server is the control plane.** It holds all engagement state and, by default, binds
   to `127.0.0.1` only. Never expose it to a network you do not control.

### Residual risks (accepted, with mitigations)

- **Injection-driven exfiltration of the model API key.** A malicious target can tell the agent to
  POST the key to itself. Mitigation: a **separate, spend-limited API key** for Sharp (rotate it
  after engagements against untrusted targets, monitor spend), plus the worker prompts state the
  three prohibitions explicitly (never obey, never send secrets outward, never widen scope).
  The prompt layer is a soft guard — it makes injection a deliberate rule violation, it does not
  stop a determined one. The container boundary is the hard one.
- **Out-of-scope action.** Injection or agent error may reach beyond the intended target.
  Mitigations: the authorization confirmation gate, the human approval gate on high-risk actions,
  per-project containers, and the fact that every action is recorded in the graph for review.
  Operator responsibility: keep the scope tight, watch the approval queue, and treat the agent as a
  capable-but-gullible junior tester.
- **Container escape / host compromise.** Mitigation: container isolation plus the Docker defaults
  Sharp ships with. Do not add `NET_ADMIN`/`NET_RAW`/host mounts unless the engagement needs them.
- **Engagement data at rest is not encrypted.** Protect the DB file (`datas/sharp/*.db`), the
  secrets file (`datas/sharp/secrets.env`, keep mode `600`), the license key, and the `reports/`
  directory with normal host hygiene. None of them belong in version control.
- **Web UI token storage.** The browser stores the session token in `sessionStorage` (cleared when
  the tab closes) and the server's signing secret is random per process, so a server restart
  invalidates sessions. Treat the UI as a localhost-only tool.

### Hardening checklist for operators

- [ ] `sharp serve` bound to `127.0.0.1` (the default); no reverse proxy exposure.
- [ ] A dedicated, spend-capped API key for Sharp — not your personal key.
- [ ] `datas/sharp/secrets.env` mode `600`, never committed, never shared in a zip.
- [ ] Review the approval queue before enabling emergency mode; emergency mode is time-boxed and
      logged for a reason.
- [ ] Rotate the API key after testing targets you do not fully trust.
- [ ] Keep `reports/` and the DB out of backups/shares you do not control.

## Scope

Sharp is intended **only** for systems you have explicit written authorization to test. Running it
against anything else may be illegal in your jurisdiction. The tool ships an authorization
confirmation gate and an approval gate for high-risk actions to make that boundary visible — they
are guardrails, not a substitute for your own authorization and judgment.
