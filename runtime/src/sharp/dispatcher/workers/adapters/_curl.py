from __future__ import annotations

import shlex
from dataclasses import dataclass


_VERBOSE_CURL_SCRIPT = """
url="$1"
payload="$2"
shift 2

body_file="$(mktemp)"
cleanup() {
    rm -f "$body_file"
}
trap cleanup EXIT

curl_rc=0
http_status="$(
    curl -sS -o "$body_file" -w '%{http_code}' "$url" "$@" -d "$payload"
)" || curl_rc=$?

printf 'http_status=%s\\n' "$http_status"
if [ -s "$body_file" ]; then
    cat "$body_file"
fi

if [ "$curl_rc" -ne 0 ]; then
    exit "$curl_rc"
fi

case "$http_status" in
    2??) exit 0 ;;
    *) exit 1 ;;
esac
""".strip()


def build_verbose_curl_healthcheck(url: str, *, headers: list[str], payload: str) -> list[str]:
    return ["/bin/sh", "-lc", _VERBOSE_CURL_SCRIPT, "--", url, payload, *headers]


@dataclass(frozen=True, slots=True)
class ShellArgument:
    text: str
    expand_env: bool = False


def expand_env(text: str) -> ShellArgument:
    return ShellArgument(text=text, expand_env=True)


def render_curl_command(url: str, *, headers: list[str | ShellArgument], payload: str) -> str:
    parts = ["curl", "-sS", url, *headers, "-d", payload]
    return " ".join(_render_shell_argument(part) for part in parts)


def _render_shell_argument(part: str | ShellArgument) -> str:
    if isinstance(part, ShellArgument):
        if part.expand_env:
            return _double_quote_with_env_expansion(part.text)
        return shlex.quote(part.text)
    return shlex.quote(part)


def _double_quote_with_env_expansion(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
    return f'"{escaped}"'


# Verbose healthcheck where the Authorization header value is expanded from an
# environment variable at exec time (never placed in argv).  The env var is
# injected by docker exec via worker.env, so the key travels env -> curl config
# file -> curl, and never appears on any command line visible to `ps`.
_ENV_CURL_SCRIPT = """
url="$1"
payload="$2"
auth_env="$3"
shift 3

cfg="$(mktemp)"
body_file="$(mktemp)"
cleanup() {
    rm -f "$cfg" "$body_file"
}
trap cleanup EXIT

printf 'header = "Authorization: Bearer %s"\n' "$(printenv "$auth_env")" > "$cfg"
printf '%s\\n' 'header = "Content-Type: application/json"' >> "$cfg"

curl_rc=0
http_status="$(
    curl -sS -o "$body_file" -w '%{http_code}' -K "$cfg" "$url" -d "$payload"
)" || curl_rc=$?

printf 'http_status=%s\\n' "$http_status"
if [ -s "$body_file" ]; then
    cat "$body_file"
fi

if [ "$curl_rc" -ne 0 ]; then
    exit "$curl_rc"
fi

case "$http_status" in
    2??) exit 0 ;;
    *) exit 1 ;;
esac
""".strip()


def build_env_curl_healthcheck(url: str, *, auth_env_key: str, payload: str) -> list[str]:
    """Healthcheck argv that reads the bearer token from the *auth_env_key*
    environment variable at exec time (curl config file) — the key never
    appears in argv.  Prints `http_status=<code>` then the body; exits 0 only
    on 2xx (same contract as the plain verbose healthcheck)."""
    return ["/bin/sh", "-lc", _ENV_CURL_SCRIPT, "--", url, payload, auth_env_key]
