"""Merge-gate tests for reason intent write retry semantics (patch #6 + IDEMP-1).

Retries are now safe against duplicates: each logical intent carries a stable
``idempotency_key`` that is reused across attempts, and the server deduplicates
on ``(project_id, idempotency_key)``. See
docs/adr/0001-reason-intent-retry-non-idempotent.md (Resolved by IDEMP-1) and
docs/tickets/IDEMP-1-intent-idempotency-key.md.
"""

from __future__ import annotations

import pytest

from sharp.dispatcher.protocol.client import ApiResult
from sharp.dispatcher.tasks import reason
from sharp.dispatcher.tasks.reason import _create_intent_with_retry


class FakeClient:
    """Records every create_intent call. When ``commit`` is set, models a server
    that persists rows but deduplicates on idempotency_key — mirroring the real
    server's partial unique index on (project_id, idempotency_key)."""

    def __init__(self, statuses: list[int], *, commit: bool = False):
        self._statuses = statuses
        self.commit = commit
        self.calls = 0
        self.keys_seen: list[str | None] = []
        self.server_intents: dict[str, str] = {}  # idempotency_key -> description

    def create_intent(
        self,
        project_id,
        from_ids,
        description,
        worker,
        idempotency_key=None,
        risk_level=None,
        risk_reason=None,
    ):
        status = self._statuses[min(self.calls, len(self._statuses) - 1)]
        self.calls += 1
        self.keys_seen.append(idempotency_key)
        if self.commit:
            # Dedup by key, exactly like the server's ON CONFLICT / pre-check path.
            self.server_intents.setdefault(idempotency_key, description)
        return ApiResult(status_code=status, data={"id": f"i{self.calls}"}, text="")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    # Keep retries instant; the backoff value itself is not under test.
    monkeypatch.setattr(reason, "INTENT_WRITE_BACKOFF_SECONDS", 0)


def _call(client: FakeClient) -> ApiResult:
    return _create_intent_with_retry(client, "p1", ["origin"], "probe web root", "w1")


def test_success_first_try_no_retry():
    client = FakeClient([201])
    result = _call(client)
    assert result.ok
    assert client.calls == 1


def test_no_retry_on_client_error():
    # 4xx is not transient: it must not be retried (would just repeat a bad request).
    client = FakeClient([400])
    result = _call(client)
    assert not result.ok
    assert client.calls == 1


def test_retry_on_5xx_then_succeeds():
    client = FakeClient([500, 201])
    result = _call(client)
    assert result.ok
    assert client.calls == 2


def test_retry_on_connection_error_status_zero():
    # status_code == 0 is the client-side "request failed / no response" marker.
    client = FakeClient([0, 0, 201])
    result = _call(client)
    assert result.ok
    assert client.calls == 3


def test_retries_are_bounded():
    # Persistent 5xx must stop after INTENT_WRITE_MAX_ATTEMPTS, not loop forever.
    client = FakeClient([500, 500, 500, 500, 500])
    result = _call(client)
    assert not result.ok
    assert client.calls == reason.INTENT_WRITE_MAX_ATTEMPTS


def test_stable_key_reused_across_retries():
    # The same idempotency_key must be sent on every attempt so the server can
    # deduplicate. A per-attempt key would defeat the whole mechanism.
    client = FakeClient([0, 0, 201])
    _call(client)
    assert client.calls == 3
    assert len(set(client.keys_seen)) == 1
    assert client.keys_seen[0] is not None


def test_lost_success_response_does_not_create_duplicate():
    """IDEMP-1 gate: first call commits server-side but its response is lost
    (status 0); the retry carries the same key, so the server resolves to the
    SAME row -> NO duplicate."""
    client = FakeClient([0, 201], commit=True)
    result = _call(client)
    assert result.ok
    assert client.calls == 2
    assert len(client.server_intents) == 1  # deduplicated by idempotency_key
