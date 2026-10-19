from __future__ import annotations

import logging
import os
import socket
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass
from pathlib import Path

import requests

from sharp.dispatcher.config import DispatchConfig, WorkerConfig
from sharp.dispatcher.models import ReasonCheckpoint, RunningTask
from sharp.dispatcher.protocol.client import SharpClient
from sharp.dispatcher.runtime.cancellation import TaskCancellation
from sharp.dispatcher.runtime.containers import ContainerManager
from sharp.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from sharp.dispatcher.scheduler.provider_health import suspension_for_outcome
from sharp.dispatcher.scheduler.worker_select import choose_worker
from sharp.dispatcher.tasks.bootstrap import run_bootstrap_task
from sharp.dispatcher.tasks.explore import run_explore_task
from sharp.dispatcher.tasks.reason import run_reason_task
from sharp.server.models import Intent, ProjectDetail, ProjectSummary
from sharp.server.reports import is_engineered_report_intent_description

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
# After how many consecutive reason cycles with no new facts the project is
# considered stalled. A diagnostic hint is written and reason dispatch is paused
# until the user adds a new hint.
REASON_STALL_THRESHOLD = 3
STALL_HINT_CREATOR = "dispatcher.stall_detector"
# Maximum task runtime in seconds (1 hour). Tasks exceeding this will be forcibly cancelled.
TASK_TIMEOUT_SECONDS = 3600
# On shutdown, how long to wait for cancelled tasks to return before proceeding
# anyway. Cancellation kills the container process (pkill -s) so tasks normally
# return in well under this; the bound only guards against a wedged worker thread
# blocking the whole shutdown.
SHUTDOWN_TASK_WAIT_SECONDS = 30


class ServerSettingsError(RuntimeError):
    """Fatal dispatcher configuration mismatch (e.g. server intent/reason
    timeout not greater than the dispatcher interval).  Unlike transient
    per-cycle RuntimeErrors (docker-daemon jitter), this must stop the
    dispatcher instead of being swallowed by per-cycle isolation."""


def _deadline_passed(deadline_iso: str) -> bool:
    """True when an ISO-8601 UTC deadline is in the past. Unparseable → False
    (never block dispatch on a malformed value)."""
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return False


def rotate_ids(ids: list[str], cursor: int) -> tuple[list[str], int]:
    """Round-robin rotation helper (pure).

    Returns ``(ids[offset:] + ids[:offset], cursor + 1)`` where
    ``offset = cursor % len(ids)`` — the next candidate to try starts where the
    previous cycle stopped, spreading dispatch fairly across projects. An empty
    list returns unchanged and does not advance the cursor.
    """
    if not ids:
        return [], cursor
    offset = cursor % len(ids)
    return ids[offset:] + ids[:offset], cursor + 1


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]


class DispatcherLoop:
    def __init__(self, config_path: Path, server_override: str | None = None):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        if server_override:
            self.config = self.config.model_copy(update={"server": server_override})
        self.dispatcher_id = os.environ.get("SHARP_DISPATCHER_ID") or f"{socket.gethostname()}:{os.getpid()}:{config_path.name}"
        self.client = SharpClient(self.config.server)
        self.container_manager = ContainerManager(self.config.container)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.reason_checkpoints: dict[str, ReasonCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False
        # Per-project counters tracking consecutive reason cycles with no new facts.
        self.reason_stall_counts: dict[str, int] = {}
        # hint_count at the time each project was diagnosed as stalled.
        # Used to detect when the user has added a new hint (clearing the stall).
        self.reason_stalled_hint_counts: dict[str, int] = {}
        # P2-3: tracks which projects have already received a budget-exhausted hint.
        self._budget_hint_written: set[str] = set()
        self._deadline_hint_written: set[str] = set()

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
            # Actively cancel in-flight tasks instead of blocking until they finish
            # on their own. cancel() -> process.kill() -> pkill -s tears down the
            # container process group, so a task that would otherwise run to its
            # inner timeout (hundreds of seconds) returns promptly.
            pending = list(self.futures.keys())
            for task in self.futures.values():
                try:
                    task.cancellation.cancel("dispatcher_shutdown")
                except Exception:
                    LOG.exception(
                        "failed to signal cancellation during shutdown project=%s task=%s",
                        task.project_id,
                        task.task_type,
                    )
            # Bounded wait: cancellation normally lands well within this window; the
            # timeout only prevents a wedged worker thread from blocking shutdown.
            _, not_done = futures_wait(pending, timeout=SHUTDOWN_TASK_WAIT_SECONDS)
            if not_done:
                LOG.warning(
                    "shutdown proceeding with %s task(s) still running after %ss",
                    len(not_done),
                    SHUTDOWN_TASK_WAIT_SECONDS,
                )
        # wait=False so a still-running task cannot block shutdown; the bounded
        # wait above already gave cancelled tasks their chance to exit.
        self.executor.shutdown(wait=False)
        self.cleanup_executor.shutdown(wait=False)
        self.container_manager.close()
        # Graceful shutdown: drop our own status row so restarts do not leave
        # stale dispatcher entries behind (each restart uses a fresh id).
        try:
            self.client.delete_self_status(self.dispatcher_id)
        except Exception:
            pass
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                        self._check_server_rev()
                    self._cancel_timed_out_tasks()
                    self._reap_futures()
                    self._reap_cleanup_futures()
                    summaries = self.client.list_projects()
                    self._initialize_reason_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._queue_container_cleanups(summaries)
                    self._dispatch_available(summaries)
                    self._publish_status(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                except ServerSettingsError:
                    # Fatal configuration mismatch (timeout <= interval etc).
                    # Must stop the dispatcher with a clear error rather than
                    # being masked as a transient cycle failure.
                    raise
                except Exception as exc:  # noqa: BLE001 - per-cycle isolation
                    # Docker-daemon jitter / container-inspect failures surface
                    # as RuntimeError from ContainerManager (it wraps
                    # DockerException), which previously escaped this loop and
                    # took the whole dispatcher process down instead of merely
                    # skipping one cycle. Treat any unexpected per-cycle error
                    # as transient: log it (with traceback for diagnosis),
                    # skip this tick, and let the next interval retry. Only
                    # genuinely fatal startup errors are raised above
                    # (startup healthchecks / settings validation).
                    if once:
                        raise
                    LOG.exception(
                        "dispatcher cycle failed (transient, skipping tick) retry_in=%ss",
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        dispatchable = [
            summary
            for summary in summaries
            if summary.status == "active" or self._summary_may_have_unclaimed_report_work(summary)
        ]
        if not dispatchable:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no dispatchable projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in dispatchable if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in dispatchable if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            if self._running_project_count(dispatchable) < self.config.runtime.max_running_projects:
                for summary in idle_projects:
                    if summary.id in self.runtime_project_ids:
                        continue
                    if self._try_dispatch_project(summary):
                        dispatched = True
                        if len(self.futures) >= self.config.runtime.max_workers:
                            return
                        break
                if dispatched:
                    continue
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(dispatchable) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(dispatchable),
                )
                return

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = sorted(summary.id for summary in summaries)
        ordered_ids, self.project_cursor = rotate_ids(ids, self.project_cursor)
        by_id = {summary.id: summary for summary in summaries}
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        # P2-4: skip paused projects for new dispatches. Running tasks are not
        # cancelled — they finish naturally. The flag is independent of status.
        if summary.paused:
            self._log_changed(
                f"{skip_scope}:paused",
                logging.INFO,
                "skip project=%s because it is paused (running tasks continue)",
                summary.id,
            )
            return False

        # Batch A: a project past its hard deadline gets no NEW work. Running
        # tasks finish naturally; the planner is expected to wrap up.
        if summary.deadline_at and _deadline_passed(summary.deadline_at):
            self._deadline_hint_once(summary.id, summary.deadline_at)
            self._log_changed(
                f"{skip_scope}:deadline",
                logging.INFO,
                "skip project=%s because its deadline passed deadline=%s",
                summary.id,
                summary.deadline_at,
            )
            return False

        # Summary-level pre-filter: skip get_project when summary data is already
        # sufficient to determine there is no dispatchable work this cycle.
        # This eliminates per-project detail requests for stable/in-flight projects.
        if not self._summary_may_need_dispatch(summary):
            self._log_changed(
                f"{skip_scope}:summary_no_work",
                logging.DEBUG,
                "skip project=%s because summary indicates no dispatchable work "
                "facts=%s hints=%s open_intents=%s reason_claimed=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                summary.working_intent_count + summary.unclaimed_intent_count,
                summary.reason is not None,
            )
            return False

        project = self.client.get_project(summary.id)

        # P2-3: check task budget. When task_budget > 0 and task_count has
        # reached the budget, skip dispatch and write a one-time hint.
        p = project.project
        if p.task_budget > 0 and p.task_count >= p.task_budget:
            self._log_changed(
                f"{skip_scope}:budget_exhausted",
                logging.INFO,
                "skip project=%s because task budget exhausted budget=%s count=%s",
                summary.id,
                p.task_budget,
                p.task_count,
            )
            self._budget_exhausted_hint_once(summary.id, p.task_budget, p.task_count)
            return False

        report_intent = self._next_report_intent(project)
        if project.project.status != "active":
            if report_intent is not None:
                # Graph snapshot is fetched AFTER the claim succeeds (batch B1) —
                # never download a full graph for a task that may not dispatch.
                return self._dispatch_explore(project, report_intent, task_type="report")
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        if self._is_initial_project(project):
            if project.project.reason is not None:
                return False
            return self._dispatch_initial_project(project)
        running_intent_ids = self._project_running_explore_intents(summary.id)
        unclaimed_intents = [
            intent
            for intent in project.intents
            if intent.to is None
            and intent.worker is None
            and intent.id not in running_intent_ids
            and not self._is_bootstrap_intent(intent)
            and intent.approval_status != "pending"  # pending must never be dispatched
            and not intent.abandoned_at  # planner retired it (batch A)
        ]
        if running_intent_ids and not unclaimed_intents:
            self._log_changed(
                f"{skip_scope}:explore_running",
                logging.DEBUG,
                "skip explore project=%s because all unclaimed intents are already running locally intents=%s",
                summary.id,
                sorted(running_intent_ids),
            )
        if unclaimed_intents:
            # Batch A: highest priority first, then oldest-first (FIFO) among
            # equals — the planner steers slots via /intents/{id}/priority.
            chosen = sorted(
                unclaimed_intents, key=lambda i: (-i.priority, i.created_at)
            )[0]
            return self._dispatch_explore(project, chosen)
        # If any intent in this project is awaiting human approval, do not run
        # a reason pass: it would only keep proposing the same high-risk intent
        # while the human reviews it. Wait for approval/rejection instead.
        if any(intent.approval_status == "pending" for intent in project.intents):
            self._log_changed(
                f"{skip_scope}:approval_pending",
                logging.INFO,
                "skip reason project=%s because %d intent(s) await human approval",
                summary.id,
                sum(1 for i in project.intents if i.approval_status == "pending"),
            )
            return False
        if project.project.reason is not None:
            self._log_changed(
                f"{skip_scope}:reason_claimed",
                logging.DEBUG,
                "skip reason project=%s because reason is already claimed by %s",
                summary.id,
                project.project.reason.worker,
            )
            return False
        reason_trigger = self._reason_trigger(project)
        if reason_trigger is None:
            self._log_changed(
                f"{skip_scope}:graph_unchanged",
                logging.DEBUG,
                "skip reason project=%s because reason state unchanged facts=%s hints=%s open_intents=%s intents=%s",
                summary.id,
                len(project.facts),
                len(project.hints),
                self._project_open_intent_count(project),
                len(project.intents),
            )
            return False
        return self._dispatch_reason(project, reason_trigger)

    def _dispatch_initial_project(self, project: ProjectDetail) -> bool:
        intent = self._get_bootstrap_intent(project)
        if intent is None:
            intent = self._create_bootstrap_intent(project.project.id)
            if intent is None:
                return False
        if self._project_has_running_bootstrap(project.project.id):
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_running",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap task is already running locally",
                project.project.id,
            )
            return False
        if intent.worker is not None:
            self._log_changed(
                f"project:{project.project.id}:skip:bootstrap_claimed",
                logging.DEBUG,
                "skip bootstrap project=%s because bootstrap intent=%s is already claimed by %s",
                project.project.id,
                intent.id,
                intent.worker,
            )
            return False
        return self._dispatch_bootstrap(project, intent)

    def _dispatch_reason(self, project: ProjectDetail, trigger: str) -> bool:
        selection = self._select_worker(project.project.id, "reason")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:reason",
                logging.INFO,
                "no worker available for reason project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:reason")
        claim = self.client.claim_reason(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "reason claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            export_yaml = self.client.export_project(project.project.id)
            future = self.executor.submit(
                run_reason_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit reason task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_reason(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "reason",
            worker.name,
            cancellation,
            start_time=time.monotonic(),
            intent_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            open_intent_count=self._project_open_intent_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        self.client.increment_task_count(project.project.id)
        LOG.info("dispatched reason project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_bootstrap(self, project: ProjectDetail, intent: Intent) -> bool:
        selection = self._select_worker(project.project.id, "bootstrap")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:bootstrap",
                logging.INFO,
                "no worker available for bootstrap project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:bootstrap")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "bootstrap claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_bootstrap_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit bootstrap task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "bootstrap", worker.name, cancellation, start_time=time.monotonic(), intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        self.client.increment_task_count(project.project.id)
        LOG.info("dispatched bootstrap project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        return True

    def _dispatch_explore(self, project: ProjectDetail, intent: Intent, *, task_type: str = "explore") -> bool:
        selection = self._select_worker(project.project.id, "explore")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:explore",
                logging.INFO,
                "no worker available for explore project=%s intent=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                intent.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:explore")
        claim = self.client.heartbeat(project.project.id, intent.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "explore claim failed project=%s intent=%s worker=%s status=%s",
                project.project.id,
                intent.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            # Fetch the graph snapshot only now that the claim is ours (batch B1):
            # a claim can still lose a race / be rejected up to this point.
            export_yaml = self.client.export_project(project.project.id)
            future = self.executor.submit(
                run_explore_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                intent,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:
            LOG.exception("failed to submit explore task project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
            self._best_effort_release(project.project.id, intent.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, task_type, worker.name, cancellation, start_time=time.monotonic(), intent_id=intent.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        self.client.increment_task_count(project.project.id)
        LOG.info("dispatched %s project=%s intent=%s worker=%s", task_type, project.project.id, intent.id, worker.name)
        return True

    def _select_worker(self, project_id: str, task_type: str) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.intent_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.intent_id}")
        summary.sort()
        return summary

    def _project_has_running_bootstrap(self, project_id: str) -> bool:
        return any(task.project_id == project_id and task.task_type == "bootstrap" for task in self.futures.values())

    def _project_running_explore_intents(self, project_id: str) -> set[str]:
        return {
            task.intent_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type in ("explore", "report") and task.intent_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        dispatchable_ids = {
            summary.id
            for summary in summaries
            if summary.status == "active" or self._summary_may_have_open_report_work(summary)
        }
        return len(self.runtime_project_ids & dispatchable_ids)

    def _project_open_intent_count(self, project: ProjectDetail) -> int:
        return sum(1 for intent in project.intents if intent.to is None)

    def _is_bootstrap_intent(self, intent: Intent) -> bool:
        return (
            intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
            and intent.from_ == ["origin"]
            and intent.to is None
        )

    def _is_report_intent(self, intent: Intent) -> bool:
        return intent.to is None and is_engineered_report_intent_description(intent.description)

    def _next_report_intent(self, project: ProjectDetail) -> Intent | None:
        running_intent_ids = self._project_running_explore_intents(project.project.id)
        intents = [
            intent
            for intent in project.intents
            if self._is_report_intent(intent)
            and intent.worker is None
            and intent.id not in running_intent_ids
        ]
        if not intents:
            return None
        return max(intents, key=lambda intent: intent.created_at)

    def _summary_may_have_unclaimed_report_work(self, summary: ProjectSummary) -> bool:
        return summary.status == "completed" and summary.unclaimed_intent_count > 0

    def _summary_may_have_open_report_work(self, summary: ProjectSummary) -> bool:
        return (
            summary.status == "completed"
            and (summary.unclaimed_intent_count > 0 or summary.working_intent_count > 0)
        )

    def _project_has_open_report_intent(self, project: ProjectDetail) -> bool:
        return any(self._is_report_intent(intent) for intent in project.intents)

    def _completed_project_has_open_report_work(self, summary: ProjectSummary) -> bool:
        if not self._summary_may_have_open_report_work(summary):
            return False
        project = self.client.get_project(summary.id)
        return self._project_has_open_report_intent(project)

    def _get_bootstrap_intent(self, project: ProjectDetail) -> Intent | None:
        intents = [intent for intent in project.intents if self._is_bootstrap_intent(intent)]
        if not intents:
            return None
        if len(intents) > 1:
            LOG.warning("project has multiple bootstrap intents project=%s intents=%s", project.project.id, [intent.id for intent in intents])
        intents.sort(key=lambda intent: (intent.worker is not None, intent.created_at, intent.id))
        return intents[0]

    def _is_initial_project(self, project: ProjectDetail) -> bool:
        fact_ids = {fact.id for fact in project.facts}
        if fact_ids != {"origin", "goal"} or len(project.facts) != 2:
            return False
        if not project.intents:
            return True
        return all(self._is_bootstrap_intent(intent) for intent in project.intents)

    def _create_bootstrap_intent(self, project_id: str) -> Intent | None:
        response = self.client.create_intent(
            project_id,
            ["origin"],
            BOOTSTRAP_INTENT_DESCRIPTION,
            BOOTSTRAP_INTENT_CREATOR,
        )
        if response.status_code == 403:
            LOG.info("project became inactive before bootstrap intent create project=%s", project_id)
            return None
        if not response.ok:
            LOG.warning(
                "bootstrap intent write failed project=%s status=%s body=%s",
                project_id,
                response.status_code,
                response.text,
            )
            return None
        if not isinstance(response.data, dict):
            LOG.warning("bootstrap intent create returned empty body project=%s", project_id)
            return None
        intent = Intent.model_validate(response.data)
        LOG.info("created bootstrap intent project=%s intent=%s", project_id, intent.id)
        return intent

    def _summary_may_need_dispatch(self, summary: ProjectSummary) -> bool:
        """Summary-level pre-filter called before get_project.

        Returns False only when the summary data is sufficient to conclude
        that this project has no dispatchable work this cycle, allowing us
        to skip the get_project HTTP request entirely.

        The filter is intentionally conservative: when in doubt it returns
        True and lets the full detail path make the final decision.
        """
        # Non-active projects enter _try_dispatch_project only when they have
        # unclaimed report intents (guarded upstream); always let them through.
        if summary.status != "active":
            return True
        # fact_count == 2 means origin + goal only: potential bootstrap candidate.
        # We need detail to inspect the bootstrap intent's claim state.
        if summary.fact_count <= 2:
            return True
        # There are intents ready to be explored.
        if summary.unclaimed_intent_count > 0:
            return True
        # Reason is already claimed by another worker and there are no unclaimed
        # intents: nothing for this dispatcher to do this cycle.
        if summary.reason is not None:
            return False
        # No reason claim, no unclaimed intents: only worthwhile if the graph
        # has changed enough to warrant a new reason pass.
        open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
        if self._reason_trigger_from_counts(
            summary.id, summary.fact_count, summary.hint_count, open_intent_count
        ) is None:
            return False
        # Even if a trigger fires, skip if the project is stalled and the user
        # hasn't added new hints since the diagnostic hint was written.
        stalled_at = self.reason_stalled_hint_counts.get(summary.id)
        if stalled_at is not None:
            # stalled_at + 1 accounts for the diagnostic hint we wrote ourselves.
            if summary.hint_count <= stalled_at + 1:
                self._log_changed(
                    f"project:{summary.id}:skip:stalled",
                    logging.INFO,
                    "skip reason project=%s because project is stalled stall_count=%s "
                    "waiting for user hint hint_count=%s stalled_at=%s",
                    summary.id,
                    self.reason_stall_counts.get(summary.id, 0),
                    summary.hint_count,
                    stalled_at,
                )
                return False
            # User added a new hint after the diagnostic — clear stall.
            self.reason_stall_counts.pop(summary.id, None)
            self.reason_stalled_hint_counts.pop(summary.id, None)
            self._clear_log_state(f"project:{summary.id}:skip:stalled")
            LOG.info(
                "reason stall cleared by user hint project=%s hint_count=%s",
                summary.id,
                summary.hint_count,
            )
        return True

    def _on_reason_stalled(self, project_id: str, stall_count: int, current_hint_count: int) -> None:
        """Called when reason has cycled REASON_STALL_THRESHOLD times without
        producing new facts. Writes a diagnostic hint and pauses reason dispatch
        until the user adds a new hint."""
        if project_id in self.reason_stalled_hint_counts:
            # Already stalled — don't write duplicate hints.
            return
        LOG.warning(
            "project stalled reason_cycles=%s no new facts project=%s "
            "writing diagnostic hint and pausing reason dispatch",
            stall_count,
            project_id,
        )
        hint_content = (
            f"[Sharp 诊断] 已连续 {stall_count} 轮探索未发现新进展。"
            "可能原因：目标有防护拦截、探索方向已穷尽、或 Goal 描述过于模糊。\n"
            "请补充以下任意一项后系统将自动恢复探索：\n"
            "• 调整探索方向（如换用被动分析、聚焦特定接口）\n"
            "• 说明目标防护机制（如有 WAF、需绕过哪种限制）\n"
            "• 补充已知漏洞线索或业务逻辑说明"
        )
        response = self.client.create_hint(project_id, hint_content, STALL_HINT_CREATOR)
        if response.ok:
            # Record hint_count at stall time so we can detect user intervention.
            self.reason_stalled_hint_counts[project_id] = current_hint_count
            LOG.info("stall diagnostic hint written project=%s", project_id)
        else:
            LOG.warning(
                "failed to write stall hint project=%s status=%s",
                project_id,
                response.status_code,
            )

    def _reason_trigger_from_counts(
        self,
        project_id: str,
        fact_count: int,
        hint_count: int,
        open_intent_count: int,
    ) -> str | None:
        """Return a trigger description if counts differ enough from the last
        reason checkpoint, or None if the graph is unchanged.

        Trigger conditions:
        - New facts have appeared (fact_count grew).
        - New hints have been added (hint_count grew).
        - Open intents decreased — any reduction, not just reaching zero.
          A partial decrease means some explores concluded (possibly without
          producing a fact, e.g. a rejected or released intent); reason should
          re-evaluate to decide whether to spawn new intents or complete.
        """
        checkpoint = self.reason_checkpoints.get(project_id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if fact_count > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{fact_count}")
        if hint_count > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{hint_count}")
        if open_intent_count < checkpoint.open_intent_count:
            changes.append(
                f"open_intents:{checkpoint.open_intent_count}->{open_intent_count}"
            )
        if not changes:
            return None
        return ",".join(changes)

    def _reason_trigger(self, project: ProjectDetail) -> str | None:
        open_intent_count = self._project_open_intent_count(project)
        return self._reason_trigger_from_counts(
            project.project.id,
            len(project.facts),
            len(project.hints),
            open_intent_count,
        )

    def _cancel_timed_out_tasks(self) -> None:
        """Cancel tasks that have exceeded TASK_TIMEOUT_SECONDS."""
        now = time.monotonic()
        for future, task in list(self.futures.items()):
            if future.done():
                continue
            elapsed = now - task.start_time
            if elapsed > TASK_TIMEOUT_SECONDS:
                LOG.warning(
                    "task timeout exceeded project=%s task=%s worker=%s elapsed=%.0fs timeout=%ds - cancelling",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    elapsed,
                    TASK_TIMEOUT_SECONDS,
                )
                task.cancellation.cancel()

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                suspension = suspension_for_outcome(outcome)
                provider_kind = suspension[0] if suspension else None
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                elif provider_kind:
                    # Provider 级故障（额度/认证/限流）：重试无意义，冷却足够久让调度器
                    # 自然换用配置里的其它 provider（P2 降级）。复用同一个冷却表，
                    # 不新增机制——只是时长从 5s 级提升到分钟级。
                    retry_after_seconds = suspension[1]
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.warning(
                        "worker suspended by provider failure worker=%s kind=%s retry_after=%.0fs",
                        task.worker_name,
                        provider_kind,
                        retry_after_seconds,
                    )
                else:
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                selection_task_type = "explore" if task.task_type == "report" else task.task_type
                rejection_key = (task.project_id, selection_task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "reason":
                    assert task.fact_count is not None
                    assert task.hint_count is not None
                    assert task.open_intent_count is not None
                    old_checkpoint = self.reason_checkpoints.get(task.project_id)
                    new_checkpoint = ReasonCheckpoint(
                        fact_count=task.fact_count,
                        hint_count=task.hint_count,
                        open_intent_count=task.open_intent_count,
                    )
                    self.reason_checkpoints[task.project_id] = new_checkpoint
                    # Stall detection: count consecutive reason cycles that produced no new facts.
                    if old_checkpoint is not None and task.fact_count <= old_checkpoint.fact_count:
                        stall = self.reason_stall_counts.get(task.project_id, 0) + 1
                        self.reason_stall_counts[task.project_id] = stall
                        LOG.debug(
                            "reason stall count project=%s stall_count=%s/%s",
                            task.project_id,
                            stall,
                            REASON_STALL_THRESHOLD,
                        )
                        if stall >= REASON_STALL_THRESHOLD:
                            self._on_reason_stalled(task.project_id, stall, task.hint_count)
                    else:
                        # New facts appeared — reset stall counter and clear any stall state.
                        if task.project_id in self.reason_stall_counts:
                            self.reason_stall_counts.pop(task.project_id, None)
                            self.reason_stalled_hint_counts.pop(task.project_id, None)
                            LOG.info("reason stall cleared project=%s facts_now=%s", task.project_id, task.fact_count)
                    LOG.debug(
                        "reason checkpoint updated project=%s facts=%s hints=%s open_intents=%s",
                        task.project_id,
                        task.fact_count,
                        task.hint_count,
                        task.open_intent_count,
                    )
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._project_running_task_count(summary.id) > 0:
                continue
            if self._completed_project_has_open_report_work(summary):
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)
        self._cleanup_orphan_containers(summaries)

    def _cleanup_orphan_containers(self, summaries: list[ProjectSummary]) -> None:
        expected = {self.container_manager.container_name(summary.id) for summary in summaries}
        for container_name in self.container_manager.managed_container_names():
            if container_name in expected:
                continue
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_orphan_cleanup(container_name):
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_orphan, container_name)
            self.cleanup_futures[future] = (container_name, None, None)
            self._cleanup_pending.add(container_name)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        dispatchable_ids = {
            summary.id
            for summary in summaries
            if summary.status == "active" or self._summary_may_have_open_report_work(summary)
        }
        self.runtime_project_ids.intersection_update(dispatchable_ids)
        for summary in summaries:
            if self._summary_may_have_open_report_work(summary):
                self._inactive_cleanup_done.pop(summary.id, None)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)
        # Clean up stall state for projects that are no longer active.
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        for project_id in list(self.reason_stall_counts):
            if project_id not in active_ids:
                self.reason_stall_counts.pop(project_id, None)
                self.reason_stalled_hint_counts.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if task.task_type == "report" and status == "completed":
                continue
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_reason_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.reason_checkpoints:
                continue
            open_intent_count = summary.working_intent_count + summary.unclaimed_intent_count
            if open_intent_count == 0:
                continue
            self.reason_checkpoints[summary.id] = ReasonCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                open_intent_count=open_intent_count,
            )
            LOG.debug(
                "reason checkpoint initialized project=%s facts=%s hints=%s open_intents=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                open_intent_count,
            )

    def _best_effort_release(self, project_id: str, intent_id: str, worker_name: str) -> None:
        response = self.client.release(project_id, intent_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s intent=%s worker=%s status=%s", project_id, intent_id, worker_name, response.status_code)

    def _best_effort_release_reason(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_reason(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("reason release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _budget_exhausted_hint_once(self, project_id: str, budget: int, count: int) -> None:
        """Write a one-time hint when the task budget is exhausted. Uses a
        per-project flag so the hint is only written once per exhaustion."""
        if project_id in self._budget_hint_written:
            return
        self._budget_hint_written.add(project_id)
        self._best_effort_create_hint(
            project_id,
            f"任务预算已耗尽（{count}/{budget}），调度器已停止分发新任务。"
            "如需继续，请在项目设置中提高预算或设置为 0（无限制）。",
        )

    def _deadline_hint_once(self, project_id: str, deadline_at: str) -> None:
        """Write a one-time wrap-up hint when a project passes its deadline."""
        if project_id in self._deadline_hint_written:
            return
        self._deadline_hint_written.add(project_id)
        self._best_effort_create_hint(
            project_id,
            f"[Sharp] 项目已到截止时间（{deadline_at}），调度器不再分发新任务。"
            "正在运行的任务会自然结束；请收敛已有结论、必要时提高高价值行动优先级，"
            "或完成项目。",
        )

    def _best_effort_create_hint(self, project_id: str, content: str) -> None:
        response = self.client.create_hint(project_id, content, "dispatcher.budget")
        if not response.ok:
            LOG.warning("budget hint write failed project=%s status=%s", project_id, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("intent_timeout", settings.intent_timeout), ("reason_timeout", settings.reason_timeout)):
            if value <= interval:
                raise ServerSettingsError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _check_server_rev(self) -> None:
        """比对 server 与 dispatcher 的代码指纹，不一致就大声警告。

        两个进程分别重启是常见失误（本项目已被咬过两次）。注意这里**只警告不阻断**：
        server 跑旧代码时 dispatcher 仍能工作，直接 raise 会把"能跑但有隐患"变成"完全不能跑"。
        """
        info = self.client.get_server_rev()
        if not info:
            return
        from sharp.server.rev import CODE_REV as LOCAL_REV

        server_rev = str(info.get("code_rev") or "")
        if not server_rev:
            return
        if server_rev != LOCAL_REV:
            LOG.warning(
                "代码版本不一致：server code_rev=%s dispatcher code_rev=%s —— "
                "两端不是同一份代码，请分别重启后再判断问题（改完代码只重启一个进程会导致"
                "新端点 404 或校验不生效这类怪现象）",
                server_rev,
                LOCAL_REV,
            )
        elif info.get("stale"):
            LOG.warning(
                "server code_rev=%s 但磁盘已变更（disk_rev=%s）—— server 跑的是旧代码，需重启 server",
                server_rev,
                info.get("disk_rev"),
            )
        else:
            LOG.info("代码版本一致 code_rev=%s", server_rev)
            if info.get("assets_changed"):
                # 前端资源改了但**不需要**重启（静态服务即时生效）：只作信息提示，
                # 不升级成警告 —— 否则"必须重启"这个信号会被稀释。
                LOG.info("server 前端资源已更新（即时生效，无需重启）assets_rev=%s", info.get("assets_rev"))

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, self.container_manager, show_commands=show_commands)
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))

    def _publish_status(self, summaries: list[ProjectSummary]) -> None:
        response = self.client.update_dispatcher_status(self.dispatcher_id, self._status_snapshot(summaries))
        if response.ok:
            self._clear_log_state("dispatcher/status")
            return
        self._log_changed(
            "dispatcher/status",
            logging.WARNING,
            "dispatcher status publish failed dispatcher=%s status=%s",
            self.dispatcher_id,
            response.status_code,
        )

    def _status_snapshot(self, summaries: list[ProjectSummary]) -> dict[str, object]:
        running_counts = self._worker_counts()
        now = time.time()
        return {
            "config_path": str(self.config_path),
            "server": self.config.server,
            "runtime": {
                "interval": self.config.runtime.interval,
                "max_workers": self.config.runtime.max_workers,
                "max_running_projects": self.config.runtime.max_running_projects,
                "max_project_workers": self.config.runtime.max_project_workers,
                "healthcheck_timeout": self.config.runtime.healthcheck_timeout,
                "prompt_group": self.config.runtime.prompt_group,
            },
            "running_task_count": len(self.futures),
            "running_tasks": [self._status_task(task) for task in self.futures.values()],
            "workers": [
                self._status_worker(worker, running_counts, now)
                for worker in self.config.workers
            ],
            "projects": [self._status_project(summary) for summary in summaries],
            "runtime_project_ids": sorted(self.runtime_project_ids),
            "cleanup_pending": sorted(self._cleanup_pending),
            "skip_reasons": self._status_skip_reasons(),
        }

    @staticmethod
    def _status_task(task: RunningTask) -> dict[str, object]:
        return {
            "project_id": task.project_id,
            "task_type": task.task_type,
            "worker": task.worker_name,
            "intent_id": task.intent_id,
            "cancelled": task.cancellation.is_cancelled,
            "cancel_reason": task.cancellation.reason,
        }

    def _status_worker(
        self,
        worker: WorkerConfig,
        running_counts: dict[str, int],
        now: float,
    ) -> dict[str, object]:
        unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
        rejected = [
            {
                "project_id": project_id,
                "task_type": task_type,
                "remaining_seconds": max(0, int(until - now)),
            }
            for (project_id, task_type, worker_name), until in sorted(self.worker_rejected_until.items())
            if worker_name == worker.name and until > now
        ]
        return {
            "name": worker.name,
            "type": worker.type,
            "task_types": list(worker.task_types),
            "priority": worker.priority,
            "running": running_counts.get(worker.name, 0),
            "max_running": worker.max_running,
            "unhealthy_remaining_seconds": max(0, int(unhealthy_until - now)) if unhealthy_until > now else 0,
            "rejected": rejected,
        }

    @staticmethod
    def _status_project(summary: ProjectSummary) -> dict[str, object]:
        return {
            "id": summary.id,
            "title": summary.title,
            "status": summary.status,
            "fact_count": summary.fact_count,
            "intent_count": summary.intent_count,
            "working_intent_count": summary.working_intent_count,
            "unclaimed_intent_count": summary.unclaimed_intent_count,
            "hint_count": summary.hint_count,
            "reason_worker": summary.reason.worker if summary.reason else None,
        }

    def _status_skip_reasons(self) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for scope, (level, message, args) in sorted(self._log_state.items()):
            if not scope.startswith(("project:", "dispatch/")):
                continue
            try:
                rendered = message % args
            except Exception:
                rendered = message
            entries.append(
                {
                    "scope": scope,
                    "level": logging.getLevelName(level),
                    "message": rendered,
                }
            )
        return entries[-50:]
