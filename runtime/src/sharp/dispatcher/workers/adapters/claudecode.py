from __future__ import annotations

import json

from sharp.dispatcher.config import WorkerConfig
from sharp.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"

# P1-3 token metering gate (worker env, default off): when set to "1", every
# claude -p invocation gains --output-format json, whose stdout carries the
# model text in the `result` field plus usage/cost metadata that Sharp records
# into the task_usage table. Requires a claude CLI version that supports the
# flag; off keeps the legacy argv byte-for-byte.
SHARP_TOKEN_METERING_ENV = "SHARP_TOKEN_METERING"

# Healthcheck runs via Node's fetch (not curl) on purpose: claude-code itself
# talks to the API through Node, so some reverse-proxy gateways (e.g. TLS/JA3
# fingerprint filtering) accept Node's client but 502 curl's OpenSSL handshake.
# Using Node keeps the healthcheck on the exact transport the real work uses.
# It prints `http_status=<code>` then the body, and exits 0 only on 2xx — same
# contract the startup-healthcheck parser and pre-task healthcheck expect.
#
# The auth token is read from the environment (ANTHROPIC_AUTH_TOKEN is injected
# by docker exec via worker.env), NOT from argv — argv is visible to any
# process on the host or in the container via `ps`.
_NODE_HEALTHCHECK_SCRIPT = """
const url = process.argv[1];
const payload = process.argv[2];
const headers = { 'content-type': 'application/json' };
if (process.env.ANTHROPIC_AUTH_TOKEN) headers['authorization'] = `Bearer ${process.env.ANTHROPIC_AUTH_TOKEN}`;
headers['anthropic-version'] = '2023-06-01';
fetch(url, { method: 'POST', headers, body: payload })
  .then(async (r) => {
    process.stdout.write(`http_status=${r.status}\\n`);
    process.stdout.write(await r.text());
    process.exit(r.status >= 200 && r.status < 300 ? 0 : 1);
  })
  .catch((e) => {
    process.stdout.write(`http_status=0\\n${e}\\n`);
    process.exit(1);
  });
""".strip()


def _node_healthcheck(worker: WorkerConfig) -> list[str]:
    env = worker.env
    payload = (
        '{"model":"' + env["ANTHROPIC_MODEL"] + '","max_tokens":10,'
        '"messages":[{"role":"user","content":"ping"}]}'
    )
    return [
        "node",
        "-e",
        _NODE_HEALTHCHECK_SCRIPT,
        "--",
        f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
        payload,
    ]


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return _node_healthcheck(worker)

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return _node_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        env = worker.env
        return (
            f"node -e <healthcheck> {env['ANTHROPIC_BASE_URL']}/v1/messages "
            "-H 'Authorization: Bearer $ANTHROPIC_AUTH_TOKEN' "
            f"-H 'anthropic-version: {ANTHROPIC_VERSION}' -H 'content-type: application/json'"
        )

    def _metering_enabled(self, worker: WorkerConfig) -> bool:
        return str(worker.env.get(SHARP_TOKEN_METERING_ENV, "")).strip() == "1"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        argv = [
            "claude",
            "--session-id",
            session,
            "--dangerously-skip-permissions",
            "-p",
        ]
        if self._metering_enabled(worker):
            argv.extend(["--output-format", "json"])
        argv.extend(["--", prompt])
        return DriverResult(
            argv=argv,
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        argv = [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
        ]
        if self._metering_enabled(worker):
            argv.extend(["--output-format", "json"])
        argv.extend(["--", prompt])
        return argv

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        # With --output-format json the model text lives inside the `result`
        # field of a JSON envelope. Unwrap it so the existing contract
        # (stdout = model response text) holds; any parse failure falls back
        # to the raw stdout exactly like the legacy text mode.
        if stdout:
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and isinstance(data.get("result"), str):
                    return data["result"]
            except Exception:
                pass
        return stdout

    def extract_usage(self, stdout: str) -> dict | None:
        """Best-effort extraction of usage/cost metadata from a metered stdout.

        Returns None for non-JSON output or any structural surprise; never
        raises. The record layer treats None as "no usage available".
        """
        try:
            data = json.loads(stdout)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        try:
            cost = float(data.get("total_cost_usd", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0
        model = data.get("model")
        if not isinstance(model, str):
            model = ""
        return {
            "usage": usage,
            "total_cost_usd": cost,
            "model": model,
        }
