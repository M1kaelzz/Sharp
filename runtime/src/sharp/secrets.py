from __future__ import annotations

import os
import re
from pathlib import Path

SECRET_KEYS = (
    # Anthropic / Claude
    "SHARP_ANTHROPIC_AUTH_TOKEN",
    "SHARP_TSEC_AGENT_TOKEN",
    "SHARP_MODEL",
    "SHARP_BASE_URL",
    # OpenAI
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    # Inflection / Pi
    "PI_API_KEY",
    "PI_MODEL",
    "PI_BASE_URL",
    # Active worker mode: anthropic | openai | inflection | (unset=all workers).
    # The settings page writes this to activate the matching worker type.
    "SHARP_WORKER_MODE",
)

# Non-sensitive config keys shown in plaintext (not masked) in status responses.
PLAINTEXT_KEYS = (
    "SHARP_MODEL",
    "SHARP_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "PI_MODEL",
    "PI_BASE_URL",
    "SHARP_WORKER_MODE",
)

ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
DEFAULT_SECRETS_FILE = Path("datas") / "sharp" / "secrets.env"


def default_secrets_file() -> Path:
    configured = os.environ.get("SHARP_SECRETS_FILE")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_SECRETS_FILE


def resolve_secrets_file(path: Path | None = None) -> Path:
    secrets_path = path or default_secrets_file()
    if secrets_path.is_absolute():
        return secrets_path
    return tool_root() / secrets_path


def tool_root() -> Path:
    configured = os.environ.get("SHARP_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()

    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for candidate in candidates:
        if _looks_like_tool_root(candidate):
            return candidate
    return Path.cwd().resolve()


def _looks_like_tool_root(path: Path) -> bool:
    return (
        (path / "dispatch.yaml").exists()
        or (path / "sharp").exists()
        or ((path / "pyproject.toml").exists() and path.name == "sharp")
    )


def load_secrets_file(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    secrets = read_secrets_file(path)
    for key, value in secrets.items():
        if override or not os.environ.get(key):
            os.environ[key] = value
    return secrets


def read_secrets_file(path: Path | None = None) -> dict[str, str]:
    secrets_path = resolve_secrets_file(path)
    if not secrets_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in secrets_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.groups()
        if key in SECRET_KEYS:
            result[key] = _decode_env_value(value)
    return result


def save_secrets(values: dict[str, str | None], path: Path | None = None) -> dict[str, str]:
    invalid = sorted(set(values) - set(SECRET_KEYS))
    if invalid:
        raise ValueError(f"unsupported secret keys: {', '.join(invalid)}")

    secrets_path = resolve_secrets_file(path)
    current = read_secrets_file(secrets_path)
    for key, value in values.items():
        if value is None:
            current.pop(key, None)
            continue
        cleaned = value.strip()
        if cleaned:
            current[key] = cleaned

    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secrets_path.parent.chmod(0o700)
    except OSError:
        pass
    payload = "\n".join(f"{key}={_encode_env_value(current[key])}" for key in SECRET_KEYS if key in current)
    if payload:
        payload += "\n"
    secrets_path.write_text(payload, encoding="utf-8")
    try:
        secrets_path.chmod(0o600)
    except OSError:
        pass
    return current


def secret_status(path: Path | None = None) -> list[dict[str, object]]:
    file_values = read_secrets_file(path)
    status = []
    for key in SECRET_KEYS:
        env_value = os.environ.get(key) or ""
        file_value = file_values.get(key) or ""
        value = env_value or file_value
        source = "environment" if env_value else "file" if file_value else "missing"
        status.append(
            {
                "key": key,
                "configured": bool(value),
                "source": source,
                "masked": value if key in PLAINTEXT_KEYS else mask_secret(value),
            }
        )
    return status


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _encode_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return f'"{escaped}"'


def _decode_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        body = stripped[1:-1]
        return body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
        return stripped[1:-1]
    return stripped
