from __future__ import annotations

import json
import uuid

from sharp.dispatcher.config import WorkerConfig
from sharp.dispatcher.workers.adapters._curl import build_env_curl_healthcheck, expand_env, render_curl_command
from sharp.dispatcher.workers.base import DriverResult, RegexSessionDriver

# Env var name carrying the API key into the exec environment (docker exec
# injects worker.env); healthchecks read the token from it at exec time so the
# key never appears in argv (where `ps` could see it).
AUTH_ENV_KEY = "OPENAI_API_KEY"

# Direct-API mode gate (worker env, default off): when set to "1",
# build_execute/build_conclude stop shelling out to the codex CLI and instead
# run an embedded node agent-loop that talks to CODEX_BASE_URL directly
# (chat completions + function calling + bash execution). Same rationale as the
# claudecode driver: the CLI's product-level content filter silently swallows
# attack-intent prompts, which turns every attack-style explore round into a
# no-op. Off keeps the legacy argv byte-for-byte.
SHARP_DIRECT_API_ENV = "SHARP_DIRECT_API"

# Node agent-loop (OpenAI chat-completions flavour). Runs inside the worker
# container:
#   node -e <script> -- --session <sid> --phase <execute|conclude> --prompt <p>
# Loop: POST /chat/completions (tools: bash) -> tool_calls -> run bash ->
# role:"tool" result -> repeat until the model stops calling tools. Session
# history persists under /tmp/sharp-sessions/<sid>.jsonl so conclude resumes
# the same run. stdout contract: JSON envelope (result/usage/total_cost_usd/
# model) by default (metering is ON unless SHARP_TOKEN_METERING=0 — codex
# previously had NO metering; plain text only when explicitly opted out).
_OPENAI_AGENT_LOOP_SCRIPT = r"""
const fs = require('fs');
const { spawnSync } = require('child_process');
const argv = process.argv.slice(1);
const grab = (name) => { const i = argv.indexOf(name); return (i >= 0 && i + 1 < argv.length) ? argv[i + 1] : null; };
const session = grab('--session');
const phase = grab('--phase');
const prompt = grab('--prompt');
const baseUrl = (process.env.CODEX_BASE_URL || '').replace(/\/+$/, '');
const token = process.env.OPENAI_API_KEY || '';
const model = process.env.CODEX_MODEL || '';
const metering = process.env.SHARP_TOKEN_METERING !== '0';
const maxRounds = parseInt(process.env.SHARP_AGENT_MAX_ROUNDS || '20', 10) || 20;
const bashTimeoutMs = (parseInt(process.env.SHARP_AGENT_BASH_TIMEOUT || '60', 10) || 60) * 1000;
// Output cap, same rationale as the claudecode driver (thinking models can
// burn a small budget entirely before any text is emitted).
const maxTokens = parseInt(process.env.SHARP_AGENT_MAX_TOKENS || '16384', 10) || 16384;
const SESSION_DIR = '/tmp/sharp-sessions';
const TOOLS = [
  { type: 'function', function: { name: 'bash', description: 'Run a shell command (curl/python/bash) inside the worker container. Use it to actually execute testing steps against the target.', parameters: { type: 'object', properties: { command: { type: 'string', description: 'shell command to run' } }, required: ['command'] } } }
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
      resp = await fetch(baseUrl + '/chat/completions', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token },
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
    const choice = (data.choices || [])[0] || {};
    const msg = choice.message || {};
    const toolCalls = msg.tool_calls || [];
    if (toolCalls.length === 0) { finalText = String(msg.content || ''); break; }
    messages.push({ role: 'assistant', content: msg.content || '', tool_calls: toolCalls });
    for (const tc of toolCalls) {
      let cmd = '';
      try {
        const input = JSON.parse(((tc.function || {}).arguments) || '{}');
        cmd = typeof input.command === 'string' ? input.command : '';
      } catch (e) {}
      let out = '';
      if (cmd) {
        const r = spawnSync('bash', ['-c', cmd], { encoding: 'utf8', timeout: bashTimeoutMs, maxBuffer: 8 * 1024 * 1024 });
        out = String(r.stdout || '') + '\n' + String(r.stderr || '');
        out = out.trim();
      }
      if (!out) out = '<no output>';
      messages.push({ role: 'tool', tool_call_id: tc.id, content: String(out).slice(0, 20000) });
    }
    save();
  }
  if (!finalText) {
    messages.push({ role: 'user', content: '请立即总结：输出你目前的所有研究、已执行的测试与观察结果（不要再调用任何工具，只输出总结文本）。' });
    try {
      const resp = await fetch(baseUrl + '/chat/completions', {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: 'Bearer ' + token },
        body: JSON.stringify({ model, max_tokens: maxTokens, messages }),
      });
      if (resp.ok) {
        const data = await resp.json();
        const choice = (data.choices || [])[0] || {};
        finalText = String(((choice.message || {}).content) || '');
      }
    } catch (e) {}
  }
  save();
  if (metering) {
    process.stdout.write(JSON.stringify({ result: finalText, usage: usage || {}, total_cost_usd: 0, model }));
  } else {
    process.stdout.write(finalText);
  }
})().catch((e) => { process.stderr.write('agent-error: ' + e + '\n'); process.exit(1); });
""".strip()


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_env_curl_healthcheck(
            self._healthcheck_url(worker),
            auth_env_key=AUTH_ENV_KEY,
            payload=self._healthcheck_payload(worker),
        )

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_env_curl_healthcheck(
            self._healthcheck_url(worker),
            auth_env_key=AUTH_ENV_KEY,
            payload=self._healthcheck_payload(worker),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return render_curl_command(
            self._healthcheck_url(worker),
            headers=[
                "-H",
                expand_env("Authorization: Bearer $OPENAI_API_KEY"),
                "-H",
                "content-type: application/json",
            ],
            payload=self._healthcheck_payload(worker),
        )

    def _direct_enabled(self, worker: WorkerConfig) -> bool:
        return str(worker.env.get(SHARP_DIRECT_API_ENV, "")).strip() == "1"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self._direct_enabled(worker):
            # Seed the session here (prepare_session stays None for the legacy
            # CLI path, which extracts the real session from stderr instead).
            # DriverResult.session flows through extract_session (which
            # short-circuits on a non-None session) into build_conclude.
            sid = str(uuid.uuid4())
            return DriverResult(
                argv=[
                    "node", "-e", _OPENAI_AGENT_LOOP_SCRIPT,
                    "--",
                    "--session", sid,
                    "--phase", "execute",
                    "--prompt", prompt,
                ],
                session=sid,
            )
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["CODEX_MODEL"],
                "-c",
                'model_provider="sharp"',
                "-c",
                'model_providers.sharp.name="sharp"',
                "-c",
                'model_providers.sharp.wire_api="responses"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                f'model_providers.sharp.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.sharp.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self._direct_enabled(worker):
            return [
                "node", "-e", _OPENAI_AGENT_LOOP_SCRIPT,
                "--",
                "--session", session,
                "--phase", "conclude",
                "--prompt", prompt,
            ]
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            session,
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="sharp"',
            "-c",
            'model_providers.sharp.name="sharp"',
            "-c",
            'model_providers.sharp.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.sharp.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.sharp.env_key="OPENAI_API_KEY"',
            "--",
            prompt,
        ]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        # Direct mode with metering prints a JSON envelope whose `result` field
        # carries the model text; unwrap it so the existing contract
        # (stdout = model response text) holds. Legacy CLI output (plain text,
        # or the event stream) falls back to the raw stdout byte-for-byte.
        if stdout:
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and isinstance(data.get("result"), str):
                    return data["result"]
            except Exception:
                pass
        return stdout

    def extract_usage(self, stdout: str) -> dict | None:
        """Direct-mode metering: best-effort usage extraction from the JSON
        envelope. Legacy CLI mode has no metering (None). Never raises."""
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

    @staticmethod
    def _healthcheck_url(worker: WorkerConfig) -> str:
        return f"{worker.env['CODEX_BASE_URL']}/responses"

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        return (
            '{"input":[{"content":"ping","role":"user"}],'
            '"model":"'
            + worker.env["CODEX_MODEL"]
            + '","stream":false}'
        )