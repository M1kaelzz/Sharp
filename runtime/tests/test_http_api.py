"""HTTP-layer (ASGI) tests (batch 10).

Business logic is covered by router-function tests (test_approval_gate.py,
test_intent_idempotency_api.py); this file locks the HTTP semantics that
function-level tests cannot see: the auth middleware boundary (server token vs
human JWT vs anonymous), route wiring + request validation (422), and the SSE
event stream.

The security invariant under test is the approval gate over HTTP:
- The server token (the AI / dispatcher) may drive the whole intent protocol
  but MUST be rejected from the human approval endpoints (403).
- A pending high-risk intent MUST NOT be claimable until a human JWT approves
  it — heartbeat then succeeds.
- Approval endpoints require an explicit project_id (422 without it).

DB is configured per-test to a tmp SQLite; the FastAPI app is driven WITHOUT
its lifespan (TestClient used bare / httpx ASGITransport), so db.configure()
in lifespan never overwrites the fixture's database.
"""

from __future__ import annotations

import asyncio

import pytest

from sharp.server import auth, db

SERVER_TOKEN = "server-token-xyz"


@pytest.fixture
def env_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None, raising=False)
    monkeypatch.setenv("SHARP_SERVER_TOKEN", SERVER_TOKEN)
    db.configure(tmp_path / "sharp.db")


@pytest.fixture
def client(env_db):
    from fastapi.testclient import TestClient
    from sharp.server.app import app

    c = TestClient(app)  # bare: no lifespan, keeps env_db's tmp DB
    yield c
    c.close()


def _server_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SERVER_TOKEN}"}


def _human_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {auth.sign_token(auth.get_jwt_key())}"}


def _create_project(client, origin: str = "http://target.example", title: str = "t") -> str:
    r = client.post(
        "/projects",
        json={"title": title, "origin": origin, "goal": "prove the target is hardened"},
        headers=_server_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()["project"]["id"]


def _create_intent(client, pid: str, description: str) -> dict:
    r = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": description, "creator": "ai_worker", "worker": "ai_worker"},
        headers=_server_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── auth boundary ─────────────────────────────────────────────────────────────

def test_health_is_public_without_auth(client):
    assert client.get("/health").status_code == 200


def test_api_requires_auth(client):
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={"title": "x", "origin": "y", "goal": "z"}).status_code == 401


# ── server-token protocol flow (low-risk, no approval needed) ─────────────────

def test_server_token_low_risk_protocol_flow(client):
    assert client.get("/projects", headers=_server_headers()).json() == []
    pid = _create_project(client)
    # list now contains the new project
    pids = [p["id"] for p in client.get("/projects", headers=_server_headers()).json()]
    assert pid in pids

    intent = _create_intent(client, pid, "probe https://target.example/robots.txt passively")
    assert intent["approval_status"] == "none"
    assert intent["risk_level"] == "low"

    claim = client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "ai_worker"},
        headers=_server_headers(),
    )
    assert claim.status_code == 200
    assert claim.json()["worker"] == "ai_worker"

    done = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={"worker": "ai_worker", "description": "robots.txt lists no secrets"},
        headers=_server_headers(),
    )
    assert done.status_code == 200


# ── approval gate over HTTP ───────────────────────────────────────────────────

HIGH_RISK = "写入 webshell 获取目标服务器权限"


def test_pending_high_risk_blocks_claim_until_human_approval(client):
    pid = _create_project(client)
    intent = _create_intent(client, pid, HIGH_RISK)
    assert intent["approval_status"] == "pending"

    # The AI (server token) cannot claim a pending intent...
    claim = client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "ai_worker"},
        headers=_server_headers(),
    )
    assert claim.status_code == 403

    # ...and cannot approve it either (approvals are JWT-only).
    approve = client.post(
        f"/approvals/{intent['id']}/approve?project_id={pid}",
        json={},
        headers=_server_headers(),
    )
    assert approve.status_code == 403

    # A human JWT approves it...
    ok = client.post(
        f"/approvals/{intent['id']}/approve?project_id={pid}",
        json={"note": "authorized test"},
        headers=_human_headers(),
    )
    assert ok.status_code == 200
    assert ok.json()["approval_status"] == "approved"

    # ...and only then can the AI claim and conclude.
    claim = client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "ai_worker"},
        headers=_server_headers(),
    )
    assert claim.status_code == 200
    done = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={"worker": "ai_worker", "description": "no webshell written — approved scan only"},
        headers=_server_headers(),
    )
    assert done.status_code == 200


def test_approval_requires_project_id(client):
    pid = _create_project(client)
    intent = _create_intent(client, pid, HIGH_RISK)
    # project_id is mandatory on approve — a project-less lookup could silently
    # hit another project's same-named intent (per-project id counters).
    r = client.post(
        f"/approvals/{intent['id']}/approve",
        json={},
        headers=_human_headers(),
    )
    assert r.status_code == 422


def test_approvals_list_rejects_server_token(client):
    pid = _create_project(client)
    _create_intent(client, pid, HIGH_RISK)
    # The AI must not read the human approval queue with its server token.
    assert client.get("/approvals", headers=_server_headers()).status_code == 403
    ok = client.get("/approvals", headers=_human_headers())
    assert ok.status_code == 200
    assert any(i["project_id"] == pid for i in ok.json())


# ── SSE stream ────────────────────────────────────────────────────────────────
#
# Driven by hand-rolled ASGI (not httpx ASGITransport): the transport awaits the
# whole app call, and an SSE StreamingResponse never returns, so ac.stream would
# hang before headers. Driving app(scope, receive, send) directly lets the test
# consume the response start, then publish() on the SAME event loop the
# subscriber is waiting on — which is exactly how the real server works.

def _asgi_scope(method: str, path: str, headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("test", 1234),
        "server": ("test", 80),
        "state": {},
    }


async def _start_response(scope: dict) -> tuple[asyncio.Task, dict]:
    """Run the app until http.response.start; return (task, start_message)."""
    from sharp.server.app import app

    send_q: asyncio.Queue = asyncio.Queue()
    receive_calls = 0

    async def receive() -> dict:
        # ASGI receive protocol: one http.request carrying the (empty) body,
        # then — for an SSE connection that stays open — block forever until
        # the client disconnects (task.cancel propagates into this coroutine).
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict) -> None:
        send_q.put_nowait(msg)

    task = asyncio.create_task(app(scope, receive, send))
    start = await asyncio.wait_for(send_q.get(), timeout=5)
    assert start["type"] == "http.response.start"
    return task, start


def test_sse_stream_404_for_unknown_project(env_db):
    async def main():
        task, start = await _start_response(_asgi_scope("GET", "/projects/ghost/stream", _server_headers()))
        assert start["status"] == 404
        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task

    asyncio.run(main())


def test_sse_stream_delivers_project_events(env_db):
    from sharp.server.events import publish

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at) VALUES (?, ?, 'active', ?)",
            ("p1", "t", "2026-09-01T00:00:00Z"),
        )

    async def main():
        from sharp.server.app import app

        send_q: asyncio.Queue = asyncio.Queue()
        receive_calls = 0

        async def send(msg: dict) -> None:
            send_q.put_nowait(msg)

        async def receive() -> dict:
            nonlocal receive_calls
            receive_calls += 1
            if receive_calls == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        scope = _asgi_scope("GET", "/projects/p1/stream", _server_headers())
        task = asyncio.create_task(app(scope, receive, send))

        # Response starts immediately with SSE headers...
        start = await asyncio.wait_for(send_q.get(), timeout=5)
        assert start["type"] == "http.response.start"
        assert start["status"] == 200
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        assert headers["content-type"].startswith("text/event-stream")

        # ...then the subscriber blocks on its queue. Push one event on the
        # same loop and read the delivered SSE frame.
        await asyncio.sleep(0.05)
        publish("p1", "fact", {"description": "probe completed"})

        seen: list[str] = []
        while True:
            msg = await asyncio.wait_for(send_q.get(), timeout=5)
            if msg["type"] == "http.response.body":
                chunk = (msg.get("body") or b"").decode("utf-8", "replace")
                if chunk:
                    seen.append(chunk)
                    if '"type": "fact"' in chunk:
                        break
        assert any("probe completed" in chunk for chunk in seen)

        task.cancel()
        with __import__("contextlib").suppress(BaseException):
            await task

    asyncio.run(main())
