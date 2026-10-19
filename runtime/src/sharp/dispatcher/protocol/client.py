from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from sharp.server.models import Intent, ProjectDetail, ProjectSummary, Settings

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class SharpClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    @property
    def timeout(self) -> float:
        """Per-request HTTP timeout in seconds. Callers that must wait for an
        in-flight request (e.g. a heartbeat lease shutting down) derive their
        own deadline from this value rather than hardcoding a matching number."""
        return self._timeout

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def fetch_env_baseline(self, project_id: str) -> list[dict]:
        """已确认的环境基线（P0-2）。Best-effort: [] on failure.

        注入到每个行动的提示词里，让新会话不必重复探测已确认的作业前提
        （实测：同一条网络预检曾被重复执行 144 次）。

        带 `with_inherited=1`：同目标**其他项目**已确认的前提也要带上（标注来源）。
        否则"同目标开新项目"会把连通性/工具可用性从头再探一遍 —— 这正是基线要消灭的那类重复，
        只不过发生在项目之间。
        """
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/baseline"),
                params={"with_inherited": "true"},
                timeout=self._timeout,
            )
            if not response.ok:
                return []
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def upsert_env_baseline(self, project_id: str, entries: list[dict]):
        """写入/更新环境基线（P0-2 修正版）。

        由 dispatcher 代 worker 写入——worker 容器里没有 Sharp 的凭据，也不该直连
        server（与 facts / vulnerabilities 同一模式：agent 只产出结论，控制面代写）。
        """
        return self._session().put(
            self._url(f"/projects/{project_id}/baseline"),
            json={"entries": entries},
            timeout=self._timeout,
        )

    def list_hypotheses(self, project_id: str, *, only_open: bool = True) -> list[dict]:
        """未验证假设（P0-3）。Best-effort: [] on failure."""
        try:
            url = self._url(f"/projects/{project_id}/hypotheses")
            if only_open:
                url += "?only_open=true"
            response = self._session().get(url, timeout=self._timeout)
            if not response.ok:
                return []
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def add_hypothesis(self, project_id: str, statement: str, premise_fact_ids: list[str] | None = None):
        return self._session().post(
            self._url(f"/projects/{project_id}/hypotheses"),
            json={
                "statement": statement,
                "premise_fact_ids": premise_fact_ids or [],
                "created_by": "reason",
            },
            timeout=self._timeout,
        )

    def update_hypothesis(
        self, project_id: str, hypothesis_id: str, status: str,
        note: str = "", result_fact_id: str | None = None,
    ):
        body: dict[str, object] = {"status": status, "note": note}
        if result_fact_id:
            body["result_fact_id"] = result_fact_id
        return self._session().patch(
            self._url(f"/projects/{project_id}/hypotheses/{hypothesis_id}"),
            json=body,
            timeout=self._timeout,
        )

    def list_sub_goals(self, project_id: str) -> list[dict]:
        """Phase-level objectives of a project (batch C). Best-effort: [] on failure."""
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/sub-goals"), timeout=self._timeout
            )
            if not response.ok:
                return []
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def add_sub_goal(self, project_id: str, title: str) -> ApiResult:
        """Planner/human adds a phase objective (batch C)."""
        return self._request_json(
            "POST", f"/projects/{project_id}/sub-goals", {"title": title, "created_by": "reason"}
        )

    def update_sub_goal(self, project_id: str, sub_goal_id: str, status: str, note: str = "") -> ApiResult:
        """Planner/human marks a phase objective done/abandoned/active (batch C)."""
        return self._request_json(
            "PATCH",
            f"/projects/{project_id}/sub-goals/{sub_goal_id}",
            {"status": status, "note": note},
        )

    def fetch_project_phase(self, project_id: str) -> dict | None:
        """Stage estimate (batch 11.3) for the reason prompt. Best-effort: any
        failure returns None so a phase outage never blocks reason dispatch."""
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/phase"), timeout=self._timeout
            )
            if not response.ok:
                return None
            return response.json()
        except Exception:
            return None

    def delete_self_status(self, dispatcher_id: str) -> None:
        """Remove this dispatcher's own status row on graceful shutdown, so the
        status page does not accumulate stale rows across restarts. Best-effort."""
        try:
            response = self._session().delete(
                self._url(f"/dispatcher/status/{dispatcher_id}"), timeout=5
            )
        except Exception:
            pass

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def fetch_knowledge(self, project_id: str) -> dict[str, Any] | None:
        """取本项目可用的跨项目知识（同目标 + 同产品），失败返回 None。

        与建项时的一次性 hint 注入互补：这里每轮 reason 都能拿到**新积累**的知识。
        """
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/knowledge"), timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def set_project_product(self, project_id: str, product: str) -> ApiResult:
        """标注项目所属产品/指纹 —— 知识复用的第二维度。"""
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/product",
            json={"product": product},
        )

    def fetch_asset_ledger(self, project_id: str) -> dict[str, Any] | None:
        """本项目目标资产的接口台账（P1-B），失败返回 None。

        与建项时的一次性资产覆盖提示互补：这里每轮都能拿到"跑到一半新登记/新评估"的接口。
        """
        try:
            response = self._session().get(
                self._url(f"/projects/{project_id}/asset-endpoints"), timeout=self._timeout
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def assess_endpoints(self, project_id: str, items: list[dict], worker: str) -> ApiResult:
        """把 worker 报来的接口评估写回台账（P1-B）。

        台账状态机此前只有"创建"没有"推进"：`status` 永远停在 `discovered`，
        覆盖报告的盲区清单只增不减。这条通道就是推进端。
        """
        return self._request_json(
            "POST",
            f"/projects/{project_id}/endpoint-assessments",
            json={"items": items, "worker": worker},
        )

    def get_server_rev(self) -> dict[str, Any] | None:
        """读服务端 /health 的代码指纹（best-effort，拿不到就返回 None）。

        用途：dispatcher 与 server 是两个进程，改代码后只重启其中一个是很常见的失误。
        比对两端 `code_rev` 能在启动时就把这种不一致喊出来，而不是等出怪现象再回头查。
        """
        try:
            response = self._session().get(self._url("/health"), timeout=5)
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def create_vulnerability(
        self,
        project_id: str,
        fact_id: str,
        title: str,
        *,
        intent_id: str | None = None,
        severity: str = "info",
        url: str = "",
        description: str = "",
        evidence: str = "",
        reproduction: str = "",
        impact: str = "",
        recommendation: str = "",
        kind: str = "vuln",
        score: int = 0,
    ) -> ApiResult:
        """Best-effort: create a vulnerability record linked to a fact. Errors
        are surfaced via ApiResult status code; the caller decides whether to
        log-and-continue or retry."""
        payload: dict[str, Any] = {
            "fact_id": fact_id,
            "title": title,
            "severity": severity,
            "url": url,
            "description": description,
            "evidence": evidence,
            "reproduction": reproduction,
            "impact": impact,
            "recommendation": recommendation,
            "kind": kind,
            "score": score,
        }
        if intent_id is not None:
            payload["intent_id"] = intent_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/vulnerabilities",
            json=payload,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json={"from": from_ids, "description": description, "worker": worker},
        )

    def create_hint(self, project_id: str, content: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/hints",
            json={"content": content, "creator": creator},
        )

    def abandon_intent(self, project_id: str, intent_id: str, reason: str) -> ApiResult:
        """Planner retires an open intent (batch A)."""
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/abandon",
            {"reason": reason},
        )

    def set_intent_priority(self, project_id: str, intent_id: str, priority: int) -> ApiResult:
        """Planner sets an open intent's scheduling priority (batch A)."""
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/priority",
            {"priority": priority},
        )

    def create_intent(
        self,
        project_id: str,
        from_ids: list[str],
        description: str,
        creator: str,
        idempotency_key: str | None = None,
        risk_level: str | None = None,
        risk_reason: str | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {
            "from": from_ids,
            "description": description,
            "creator": creator,
            "worker": None,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        # Model annotation is advisory; server re-checks keywords authoritatively.
        if risk_level is not None:
            payload["risk_level"] = risk_level
        if risk_reason:
            payload["risk_reason"] = risk_reason
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json=payload,
        )

    def update_dispatcher_status(self, dispatcher_id: str, data: dict[str, Any]) -> ApiResult:
        return self._request_json(
            "POST",
            "/dispatcher/status",
            json={"dispatcher_id": dispatcher_id, "data": data},
        )

    def push_live_output(
        self,
        project_id: str,
        intent_id: str,
        worker: str,
        text: str,
    ) -> None:
        """Best-effort: forward a chunk of worker stdout to SSE subscribers.
        Errors are silently swallowed — live output is non-critical."""
        try:
            self._request_json(
                "POST",
                "/dispatcher/worker-output",
                json={
                    "project_id": project_id,
                    "intent_id": intent_id,
                    "worker": worker,
                    "text": text,
                },
            )
        except Exception:
            pass  # non-critical, never block the worker for this

    def increment_task_count(self, project_id: str) -> None:
        """Best-effort: increment the task counter on the server. Errors are
        swallowed — budget tracking is advisory, not critical."""
        try:
            self._request_json(
                "POST",
                f"/projects/{project_id}/increment-task-count",
                json={},
            )
        except Exception:
            pass

    def extract_knowledge(
        self,
        project_id: str,
        fact_description: str,
    ) -> ApiResult:
        """Best-effort: ask the server to extract cross-project knowledge from
        a fact description and store it in the knowledge_base. The server
        resolves root_domain from the project's origin fact."""
        return self._request_json(
            "POST",
            "/knowledge/extract",
            json={
                "project_id": project_id,
                "description": fact_description,
            },
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                data = response.json()
            except ValueError as exc:
                # A malformed JSON body must degrade to an empty-body result,
                # not escape as a non-RequestException into callers (e.g. the
                # heartbeat thread) that only expect ApiResult.
                LOG.warning("response decode failed method=%s path=%s error=%s", method, path, exc)
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            from sharp.server.auth import server_token
            tok = server_token()
            if tok:
                session.headers["Authorization"] = f"Bearer {tok}"
        except Exception:
            pass
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
