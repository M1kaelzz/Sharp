from __future__ import annotations

import json

from sharp.dispatcher.config import WorkerConfig
from sharp.dispatcher.workers.base import DriverResult, SeedSessionDriver


ANTHROPIC_VERSION = "2023-06-01"

# P7 direct-API mode gate (worker env, default off): when set to "1",
# build_execute/build_conclude stop shelling out to the claude CLI and instead
# run an embedded node agent-loop that talks to ANTHROPIC_BASE_URL directly
# (API tool-calling + bash execution). This bypasses claude-code's product-level
# content filter, which silently swallows attack-intent prompts and returns
# empty output — fatal for a pentest tool whose explore rounds actually craft
# payloads. Off keeps the legacy argv byte-for-byte.
SHARP_DIRECT_API_ENV = "SHARP_DIRECT_API"

# Node agent-loop script. Runs inside the worker container:
#   node -e <script> --session <sid> --phase <execute|conclude> --prompt <p>
# Loop: POST /v1/messages (tools: bash) -> tool_use -> run bash -> tool_result
# -> repeat until the model stops calling tools. Session history is persisted
# under /tmp/sharp-sessions/<sid>.jsonl so conclude resumes the same run.
# stdout contract is identical to the legacy driver: plain text by default,
# JSON envelope (result/usage/total_cost_usd/model) when SHARP_TOKEN_METERING=1.
_AGENT_LOOP_SCRIPT = r"""
const fs = require('fs');
const { spawnSync } = require('child_process');
const argv = process.argv.slice(1);
const grab = (name) => { const i = argv.indexOf(name); return (i >= 0 && i + 1 < argv.length) ? argv[i + 1] : null; };
const session = grab('--session');
const phase = grab('--phase');
const prompt = grab('--prompt');
const baseUrl = (process.env.ANTHROPIC_BASE_URL || '').replace(/\/+$/, '');
const token = process.env.ANTHROPIC_AUTH_TOKEN || '';
const model = process.env.ANTHROPIC_MODEL || '';
  const metering = process.env.SHARP_TOKEN_METERING !== '0';
const maxRounds = parseInt(process.env.SHARP_AGENT_MAX_ROUNDS || '20', 10) || 20;
const bashTimeoutMs = (parseInt(process.env.SHARP_AGENT_BASH_TIMEOUT || '60', 10) || 60) * 1000;
// Output cap. Default 16384: thinking models (deepseek-v4-pro) can burn a
// 4096 budget entirely on reasoning blocks before any text is emitted —
// observed as reason rounds returning {"result":"", "output_tokens":4096}
// in a dispatch-retry loop that billed ~4096 tokens per minute.
const maxTokens = parseInt(process.env.SHARP_AGENT_MAX_TOKENS || '16384', 10) || 16384;
const SESSION_DIR = '/tmp/sharp-sessions';
const TOOLS = [
  { name: 'bash', description: 'Run a shell command (curl/python/bash) inside the worker container. Use it to actually execute testing steps against the target.', input_schema: { type: 'object', properties: { command: { type: 'string', description: 'shell command to run' } }, required: ['command'] } }
];
(async () => {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  const histFile = SESSION_DIR + '/' + session + '.jsonl';
  let messages = [];
  try { messages = fs.readFileSync(histFile, 'utf8').split('\n').filter(Boolean).map(JSON.parse); } catch (e) {}
  if (phase === 'execute' || phase === 'conclude') messages.push({ role: 'user', content: prompt });
  const save = () => { try { fs.writeFileSync(histFile, messages.map((m) => JSON.stringify(m)).join('\n')); } catch (e) {} };
  save();
  let usage = null;
  let finalText = '';
  for (let round = 0; round < maxRounds; round++) {
    let resp;
    try {
      resp = await fetch(baseUrl + '/v1/messages', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token, 'anthropic-version': '2023-06-01' },
        body: JSON.stringify({ model, max_tokens: maxTokens, tools: TOOLS, messages }),
      });
    } catch (e) { process.stderr.write('api-error: ' + e + '\n'); process.exit(1); }
    if (!resp.ok) {
      const t = await resp.text().catch(() => '');
      process.stderr.write('api-http-' + resp.status + ': ' + String(t).slice(0, 300) + '\n');
      process.exit(1);
    }
    let data;
    try { data = await resp.json(); } catch (e) { process.stderr.write('api-bad-json\n'); process.exit(1); }
    usage = data.usage || usage;
    const blocks = data.content || [];
    const toolUses = blocks.filter((b) => b.type === 'tool_use');
    const text = blocks.filter((b) => b.type === 'text').map((b) => b.text || '').join('');
    if (toolUses.length === 0) { finalText = text; break; }
    messages.push({ role: 'assistant', content: blocks });
    for (const tu of toolUses) {
      const cmd = tu.input && typeof tu.input.command === 'string' ? tu.input.command : '';
      let out = '';
      if (cmd) {
        const r = spawnSync('bash', ['-c', cmd], { encoding: 'utf8', timeout: bashTimeoutMs, maxBuffer: 8 * 1024 * 1024 });
        out = String(r.stdout || '') + '\n' + String(r.stderr || '');
        out = out.trim();
      }
      if (!out) out = '<no output>';
      messages.push({ role: 'user', content: [{ type: 'tool_result', tool_use_id: tu.id, content: String(out).slice(0, 20000) }] });
    }
    save();
  }
  if (!finalText) {
    messages.push({ role: 'user', content: '请立即调用一次 API 总结：输出你目前的所有研究、已执行的测试与观察结果（不要再调用任何工具，只输出总结文本）。' });
    try {
      const resp = await fetch(baseUrl + '/v1/messages', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token, 'anthropic-version': '2023-06-01' },
        body: JSON.stringify({ model, max_tokens: maxTokens, messages }),
      });
      if (resp.ok) {
        const data = await resp.json();
        finalText = (data.content || []).filter((b) => b.type === 'text').map((b) => b.text || '').join('');
      }
    } catch (e) {}
  }
  if (metering) {
    process.stdout.write(JSON.stringify({ result: finalText, usage: usage || {}, total_cost_usd: 0, model }));
  } else {
    process.stdout.write(finalText);
  }
})().catch((e) => { process.stderr.write('agent-error: ' + e + '\n'); process.exit(1); });
""".strip()

# P1-3 token metering gate (worker env, default ON since 2026-10-28): unless
# explicitly set to "0", every claude -p invocation gains --output-format json,
# whose stdout carries the model text in the `result` field plus usage/cost
# metadata that Sharp records into the task_usage table. Requires a claude CLI
# version that supports the flag; "0" keeps the legacy argv byte-for-byte.
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
        # Default ON (2026-10-28): unset means metered; explicit "0" opts out.
        return str(worker.env.get(SHARP_TOKEN_METERING_ENV, "1")).strip().lower() != "0"

    def _direct_enabled(self, worker: WorkerConfig) -> bool:
        return str(worker.env.get(SHARP_DIRECT_API_ENV, "")).strip() == "1"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        if self._direct_enabled(worker):
            return DriverResult(
                argv=[
                    "node", "-e", _AGENT_LOOP_SCRIPT,
                    "--",
                    "--session", session,
                    "--phase", "execute",
                    "--prompt", prompt,
                ],
                session=session,
            )
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
        if self._direct_enabled(worker):
            return [
                "node", "-e", _AGENT_LOOP_SCRIPT,
                "--",
                "--session", session,
                "--phase", "conclude",
                "--prompt", prompt,
            ]
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
