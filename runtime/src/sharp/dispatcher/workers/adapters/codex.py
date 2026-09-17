from __future__ import annotations

from sharp.dispatcher.config import WorkerConfig
from sharp.dispatcher.workers.adapters._curl import build_env_curl_healthcheck, expand_env, render_curl_command
from sharp.dispatcher.workers.base import DriverResult, RegexSessionDriver

# Env var name carrying the API key into the exec environment (docker exec
# injects worker.env); healthchecks read the token from it at exec time so the
# key never appears in argv (where `ps` could see it).
AUTH_ENV_KEY = "OPENAI_API_KEY"


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

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
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
