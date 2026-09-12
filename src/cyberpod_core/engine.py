from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from .errors import CoreError, ProviderCancelled
from .models import (
    AccessGrant, CreateSession, Evaluation, Event, Failure, FlagSubmission,
    Operation, Principal, Progress, ResourceRef, RuntimeSnapshot, Session,
    Status, context_for, utcnow,
)
from .ports import AccessBroker, Runtime, Validator
from .registry import LabRegistry, digest
from .repository import Repository

log = logging.getLogger("cyberpod.core")

TRANSITIONS = {
    Status.CREATED: {Status.STARTING, Status.STOPPING, Status.RESETTING, Status.CLEANING, Status.ERROR},
    Status.STARTING: {Status.RUNNING, Status.STOPPING, Status.ERROR},
    Status.RUNNING: {Status.STOPPING, Status.RESETTING, Status.CLEANING, Status.ERROR},
    Status.STOPPING: {Status.STOPPED, Status.ERROR},
    Status.STOPPED: {Status.STARTING, Status.RESETTING, Status.CLEANING, Status.ERROR},
    Status.RESETTING: {Status.STARTING, Status.CLEANING, Status.ERROR},
    Status.CLEANING: {Status.CLEANED, Status.ERROR},
    Status.ERROR: {Status.STOPPING, Status.RESETTING, Status.CLEANING, Status.ERROR},
    Status.CLEANED: set(),
}


class Engine:
    def __init__(self, registry: LabRegistry, repository: Repository, runtime: Runtime,
                 validators: dict[str, Validator] | None = None,
                 access_broker: AccessBroker | None = None, *,
                 clock=utcnow, startup_timeout=120.0, operation_timeout=30.0,
                 integration_timeout=15.0, max_per_owner=3, max_active=100):
        self.registry, self.repo, self.runtime = registry, repository, runtime
        self.validators = validators or {}
        self.access_broker = access_broker
        self.clock = clock
        self.startup_timeout, self.operation_timeout = startup_timeout, operation_timeout
        self.integration_timeout = integration_timeout
        self.max_per_owner, self.max_active = max_per_owner, max_active
        self._locks = WeakValueDictionary()
        self._create_lock = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}
        self._closing = False

    def _lock(self, sid):
        sid = str(sid)
        lock = self._locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sid] = lock
        return lock

    def get(self, sid: str, principal: Principal) -> Session:
        session = self.repo.get(str(sid))
        if session.owner_id != principal.subject and "admin" not in principal.scopes:
            # Hide existence from other students.
            raise CoreError("SESSION_NOT_FOUND", "Session not found", 404)
        return session

    def _event(self, session, event_type, data=None, source="core"):
        return Event(event_id=uuid4(), session_id=session.session_id, lab_id=session.lab_id,
                     generation=session.generation, type=event_type, source=source,
                     occurred_at=self.clock(), data=data or {})

    def _persist(self, session, status=None, event_type=None, data=None, evaluation=None):
        previous = session.status
        if status is not None and status != previous:
            if status not in TRANSITIONS[previous]:
                raise RuntimeError(f"Illegal lifecycle transition {previous} -> {status}")
            session.status = status
        session.updated_at = self.clock()
        event = None
        if event_type or session.status != previous:
            event = self._event(session, event_type or f"SESSION_{session.status.value}",
                                data or {"previous_status": previous.value, "status": session.status.value},
                                source="validator" if evaluation else "core")
        self.repo.save(session, event, evaluation)
        if event:
            log.info("session_event", extra={"event_type": event.type, "session_id": str(session.session_id),
                                             "generation": session.generation, "status": session.status.value})

    def availability(self, lab):
        if not lab.enabled:
            return "DISABLED", "LAB_DISABLED"
        if lab.validator and lab.validator.id not in self.validators:
            return "UNAVAILABLE", "VALIDATOR_UNAVAILABLE"
        if (lab.browser.required or lab.terminal.required) and self.access_broker is None:
            return "UNAVAILABLE", "ACCESS_UNAVAILABLE"
        try:
            self.runtime.validate_lab(lab)
        except Exception:
            return "UNAVAILABLE", "RUNTIME_UNSUPPORTED"
        return "AVAILABLE", None

    async def create(self, lab_id: str, principal: Principal, request: CreateSession,
                     idempotency_key: str | None = None) -> tuple[Session, bool]:
        if self._closing:
            raise CoreError("SHUTTING_DOWN", "Core is shutting down", 503)
        if "student" not in principal.scopes and "admin" not in principal.scopes:
            raise CoreError("FORBIDDEN", "Student scope required", 403)
        if idempotency_key is not None and (not 1 <= len(idempotency_key) <= 128
                                           or not idempotency_key.isascii()
                                           or not idempotency_key.isprintable()):
            raise CoreError("INVALID_IDEMPOTENCY_KEY", "Invalid Idempotency-Key", 422)
        fingerprint = digest({"lab_id": lab_id, **request.model_dump(mode="json")})
        async with self._create_lock:
            if idempotency_key:
                replay = self.repo.replay(principal.subject, idempotency_key, fingerprint)
                if replay:
                    return replay, False
            lab = self.registry.get(lab_id)
            availability, reason = self.availability(lab)
            if availability != "AVAILABLE":
                raise CoreError(reason, "Lab is not available", 503)
            ttl = request.ttl_seconds if request.ttl_seconds is not None else lab.session_timeout_seconds
            if ttl > lab.session_timeout_seconds:
                raise CoreError("TTL_EXCEEDS_LIMIT", "TTL exceeds the configured lab limit", 422)
            active = [s for s in self.repo.all() if s.status != Status.CLEANED]
            if len(active) >= self.max_active or sum(s.owner_id == principal.subject for s in active) >= self.max_per_owner:
                raise CoreError("SESSION_QUOTA", "Active session quota reached", 429)
            now = self.clock()
            session = Session(session_id=uuid4(), owner_id=principal.subject, lab_id=lab.id,
                              lab_version=lab.version, definition_hash=digest(lab.model_dump(mode="json")),
                              definition=lab, runtime_name=self.runtime.name, deployment_id=self.repo.deployment_id,
                              created_at=now, updated_at=now, expires_at=now + timedelta(seconds=ttl))
            self.repo.create(session, self._event(session, "SESSION_CREATED"), idempotency_key, fingerprint)
            return session, True

    def _live(self, session):
        if self.clock() >= session.expires_at:
            raise CoreError("SESSION_EXPIRED", "Session has expired", 410)
        if session.status == Status.CLEANED:
            raise CoreError("SESSION_CLEANED", "Session was cleaned; create a new session", 409)

    def _runtime_matches(self, session):
        if session.runtime_name != self.runtime.name:
            raise CoreError("RUNTIME_MISMATCH", "Session requires its original runtime adapter", 503)

    async def command(self, sid: str, action: str, principal: Principal | None = None) -> Session:
        if action not in {"start", "stop", "restart", "cleanup"}:
            raise CoreError("UNKNOWN_ACTION", "Unknown lifecycle action", 404)
        sid = str(sid)
        if self._closing:
            raise CoreError("SHUTTING_DOWN", "Core is shutting down", 503)
        async with self._lock(sid):
            session = self.get(sid, principal) if principal else self.repo.get(sid)
            self._runtime_matches(session)
            if action in {"start", "restart"}:
                self._live(session)
            if session.status == Status.CLEANED and action in {"stop", "cleanup"}:
                return session
            busy = sid in self._jobs and not self._jobs[sid].done()
            if busy:
                if session.operation and session.operation.action == action:
                    return session
                if action in {"stop", "cleanup"} and session.operation and session.operation.action in {"start", "restart", "stop"}:
                    session.stop_requested = True
                    session.cleanup_requested |= action == "cleanup"
                    self._cancels[sid].set()
                    status = Status.STOPPING if session.status == Status.STARTING else None
                    self._persist(session, status, "STOP_REQUESTED", {"cleanup": session.cleanup_requested})
                    return session
                raise CoreError("OPERATION_IN_PROGRESS", "Another lifecycle operation is in progress")
            if action == "start" and session.status == Status.RUNNING:
                return session
            if action == "stop" and session.status == Status.STOPPED:
                return session
            if action == "start" and session.status not in {Status.CREATED, Status.STOPPED}:
                raise CoreError("INVALID_STATE", "Start requires CREATED or STOPPED; restart or cleanup an ERROR session")
            if action in {"start", "restart"}:
                available, reason = self.availability(session.definition)
                if available != "AVAILABLE":
                    raise CoreError(reason, "Session dependencies are unavailable", 503)
            session.operation = Operation(operation_id=uuid4(), action=action, started_at=self.clock())
            session.stop_requested = False
            session.cleanup_requested = action == "cleanup"
            session.error = None
            session.endpoints = []
            initial = {"start": Status.STARTING, "stop": Status.STOPPING,
                       "restart": Status.RESETTING, "cleanup": Status.CLEANING}[action]
            self._persist(session, initial)
            cancel = asyncio.Event()
            self._cancels[sid] = cancel
            task = asyncio.create_task(self._run(sid, action, cancel), name=f"session:{sid}:{action}")
            self._jobs[sid] = task
            task.add_done_callback(lambda finished: self._job_done(sid, finished))
            return session

    def _job_done(self, sid, task):
        if self._jobs.get(sid) is task:
            self._jobs.pop(sid, None)
            self._cancels.pop(sid, None)
        if not task.cancelled() and task.exception() is not None:
            # Do not log provider exceptions, command text or secrets.
            log.error("worker_unhandled", extra={"session_id": sid, "error_type": type(task.exception()).__name__})

    async def _call(self, awaitable, timeout=None):
        async with asyncio.timeout(timeout or self.operation_timeout):
            return await awaitable

    def _validate_resources(self, session, resources):
        expected = context_for(session).labels
        ids = set()
        logical_ids = set()
        for resource in resources:
            key = (resource.kind, resource.provider_id)
            logical_key = (resource.kind, resource.logical_id)
            if key in ids or logical_key in logical_ids or not all(resource.labels.get(k) == v for k, v in expected.items()):
                raise CoreError("RESOURCE_OWNERSHIP", "Runtime returned inconsistent resource ownership", 503)
            ids.add(key)
            logical_ids.add(logical_key)

    def _validate_ready(self, session, snapshot):
        self._validate_resources(session, snapshot.resources)
        expected = {(kind, item.id) for kind, group in [
            ("container", session.definition.containers), ("network", session.definition.networks),
            ("volume", session.definition.volumes)] for item in group}
        actual = {(r.kind, r.logical_id) for r in snapshot.resources}
        if snapshot.health != "READY" or not expected <= actual:
            raise CoreError("RUNTIME_NOT_READY", "Runtime did not confirm all required resources ready", 503)
        endpoints = {endpoint.kind for endpoint in snapshot.endpoints}
        if any(getattr(session.definition, kind).required and kind not in endpoints for kind in ("browser", "terminal")):
            raise CoreError("ACCESS_NOT_READY", "Required access endpoint is unavailable", 503)

    async def _report(self, sid, generation, resource: ResourceRef):
        async with self._lock(sid):
            session = self.repo.get(sid)
            if session.generation != generation or session.status not in {Status.STARTING, Status.STOPPING}:
                raise CoreError("STALE_RESOURCE", "Resource report is for an inactive operation")
            self._validate_resources(session, [resource])
            session.resources = [r for r in session.resources if (r.kind, r.provider_id) != (resource.kind, resource.provider_id)]
            session.resources.append(resource.model_copy(deep=True))
            self._persist(session)

    async def _run(self, sid, action, cancel):
        try:
            if action == "start":
                await self._start(sid, cancel)
            elif action == "stop":
                await self._stop(sid)
            elif action == "cleanup":
                await self._cleanup(sid)
            else:
                await self._restart(sid, cancel)
        except asyncio.CancelledError:
            async with self._lock(sid):
                session = self.repo.get(sid)
                session.operation = None
                session.endpoints = []
                session.cleanup_requested = True
                session.error = Failure(code="OPERATION_INTERRUPTED", message="Operation interrupted; cleanup required")
                self._persist(session, Status.ERROR)
        except Exception as exc:
            await self._failure(sid, action, exc)

    async def _start(self, sid, cancel):
        session = self.repo.get(sid)
        context = context_for(session)
        try:
            snapshot = await self._call(self.runtime.start(
                context, cancel, lambda resource: self._report(sid, context.generation, resource)), self.startup_timeout)
        except ProviderCancelled:
            if not cancel.is_set():
                raise
            snapshot = None
        async with self._lock(sid):
            session = self.repo.get(sid)
            expired = self.clock() >= session.expires_at
            if expired:
                session.cleanup_requested = True
            stopping = cancel.is_set() or session.stop_requested or expired
            if stopping:
                self._persist(session, Status.STOPPING)
            else:
                self._validate_ready(session, snapshot)
                session.resources, session.endpoints = snapshot.resources, snapshot.endpoints
                session.operation = None
                self._persist(session, Status.RUNNING)
        if stopping:
            await self._stop(sid)

    async def _stop(self, sid):
        context = context_for(self.repo.get(sid))
        await self._call(self.runtime.stop(context))
        snapshot = await self._call(self.runtime.inspect(context))
        async with self._lock(sid):
            session = self.repo.get(sid)
            self._validate_resources(session, snapshot.resources)
            if snapshot.health != "STOPPED" or snapshot.endpoints:
                raise CoreError("STOP_UNCONFIRMED", "Runtime did not confirm stopped workloads", 503)
            session.resources, session.endpoints = snapshot.resources, []
            cleanup = session.cleanup_requested or self.clock() >= session.expires_at
            if not cleanup:
                session.operation = None
            self._persist(session, Status.STOPPED)
            if cleanup:
                self._persist(session, Status.CLEANING)
        if cleanup:
            await self._cleanup(sid)

    async def _destroy(self, sid) -> RuntimeSnapshot:
        session = self.repo.get(sid)
        self._runtime_matches(session)
        self._validate_resources(session, session.resources)
        context = context_for(session)
        await self._call(self.runtime.cleanup(context))
        inventory = await self._call(self.runtime.inspect(context))
        self._validate_resources(session, inventory.resources)
        if inventory.resources or inventory.endpoints or inventory.health != "STOPPED":
            raise CoreError("CLEANUP_INCOMPLETE", "Runtime still reports session resources", 503)
        return inventory

    async def _cleanup(self, sid):
        await self._destroy(sid)
        async with self._lock(sid):
            session = self.repo.get(sid)
            session.resources, session.endpoints = [], []
            session.cleanup_requested = False
            session.stop_requested = False
            session.operation = None
            session.error = None
            self._persist(session, Status.CLEANED)

    async def _restart(self, sid, cancel):
        # Reset has its own state; CLEANED remains terminal. Destroy old attempt first.
        await self._destroy(sid)
        async with self._lock(sid):
            session = self.repo.get(sid)
            session.resources, session.endpoints = [], []
            if session.cleanup_requested or self.clock() >= session.expires_at:
                self._persist(session, Status.CLEANING)
                cleanup = True
            else:
                cleanup = False
                session.generation += 1
                session.progress = Progress()
                self._persist(session, Status.STARTING, "SESSION_RESTARTED",
                              {"generation": session.generation})
        if cleanup:
            await self._cleanup(sid)
        else:
            await self._start(sid, cancel)

    async def _failure(self, sid, action, exception):
        # Attempt compensation after partial start/reset. Never mark CLEANED on uncertainty.
        cleanup_ok = False
        if action in {"start", "restart"}:
            try:
                await self._destroy(sid)
                cleanup_ok = True
            except Exception:
                pass
        async with self._lock(sid):
            session = self.repo.get(sid)
            session.operation, session.endpoints = None, []
            if cleanup_ok:
                session.resources = []
            session.cleanup_requested = not cleanup_ok
            code = "RUNTIME_TIMEOUT" if isinstance(exception, TimeoutError) else "RUNTIME_OPERATION_FAILED"
            session.error = Failure(code=code, message=f"Session {action} failed; retry cleanup or restart")
            self._persist(session, Status.ERROR, "SESSION_ERROR", {"code": code, "action": action})
        log.error("runtime_failure", extra={"session_id": sid, "action": action,
                                          "error_type": type(exception).__name__, "compensated": cleanup_ok})

    async def reconcile(self):
        """Crash recovery. Interrupted commands are cleaned; running sessions are verified."""
        for session in self.repo.all():
            if session.status == Status.CLEANED:
                continue
            sid = str(session.session_id)
            if sid in self._jobs:
                continue
            try:
                self._runtime_matches(session)
                if session.operation or session.status in {Status.STARTING, Status.STOPPING, Status.RESETTING, Status.CLEANING}:
                    session.operation = None
                    session.cleanup_requested = True
                    session.endpoints = []
                    session.error = Failure(code="RECOVERY_REQUIRED", message="Interrupted operation requires cleanup")
                    self._persist(session, Status.ERROR)
                    await self.command(sid, "cleanup")
                    await self.wait_idle(sid)
                elif session.status == Status.RUNNING and self.clock() < session.expires_at:
                    snapshot = await self._call(self.runtime.inspect(context_for(session)))
                    self._validate_ready(session, snapshot)
                    session.resources, session.endpoints = snapshot.resources, snapshot.endpoints
                    self._persist(session)
            except Exception as exc:
                await self._failure(sid, "reconcile", exc)
        await self.sweep_once(check_health=False)

    async def sweep_once(self, *, check_health=True):
        """Expiry, cleanup retry and runtime liveness, bounded per provider call."""
        candidates = []
        for session in self.repo.all():
            if session.status == Status.CLEANED:
                continue
            sid = str(session.session_id)
            try:
                if self.clock() >= session.expires_at or session.cleanup_requested:
                    await self.command(sid, "cleanup")
                elif check_health and session.status == Status.RUNNING and sid not in self._jobs:
                    candidates.append(session)
            except CoreError as exc:
                log.warning("sweep_deferred", extra={"session_id": sid, "error_code": exc.code})
            except Exception as exc:
                log.error("sweep_failed", extra={"session_id": sid, "error_type": type(exc).__name__})
        # Schedule expiry for every session before probing potentially slow runtimes.
        slots = asyncio.Semaphore(8)
        async with asyncio.TaskGroup() as group:
            for session in candidates:
                group.create_task(self._probe(session, slots))

    async def _probe(self, session, slots):
        sid = str(session.session_id)
        error = None
        async with slots:
            try:
                snapshot = await self._call(self.runtime.inspect(context_for(session)), min(3.0, self.operation_timeout))
                self._validate_ready(session, snapshot)
            except Exception:
                error = Failure(code="RUNTIME_UNHEALTHY", message="Runtime health could not be verified")
            async with self._lock(sid):
                current = self.repo.get(sid)
                if current.revision != session.revision or current.status != Status.RUNNING or sid in self._jobs:
                    return
                if error:
                    current.endpoints = []
                    current.cleanup_requested = True
                    current.error = error
                    self._persist(current, Status.ERROR, "SESSION_ERROR", {"code": error.code})
                    log.error("runtime_unhealthy", extra={"session_id": sid})

    async def apply_evaluation(self, sid: str, result: Evaluation):
        sid = str(sid)
        async with self._lock(sid):
            session = self.repo.get(sid)
            self._live(session)
            if session.status != Status.RUNNING or session.operation:
                raise CoreError("SESSION_NOT_RUNNING", "Evaluation requires a running session")
            if result.session_id != session.session_id or result.lab_id != session.lab_id or result.generation != session.generation:
                raise CoreError("EVALUATION_SCOPE", "Evaluation does not match this session, lab and attempt")
            payload_hash = digest(result.model_dump(mode="json"))
            previous = self.repo.evaluation_hash(sid, result.generation, str(result.result_id))
            if previous is not None:
                if previous != payload_hash:
                    raise CoreError("EVALUATION_CONFLICT", "Result ID was reused with different content")
                return session
            if result.revision <= session.progress.revision:
                raise CoreError("STALE_EVALUATION", "Evaluation revision is stale")
            if not set(result.completed_tasks) <= {t.id for t in session.definition.tasks}:
                raise CoreError("UNKNOWN_TASK", "Evaluation contains an unknown task", 422)
            if not set(result.flags) <= {f.id for f in session.definition.flags}:
                raise CoreError("UNKNOWN_FLAG", "Evaluation contains an unknown flag", 422)
            # Store the evaluator's absolute snapshot. No points or educational rules here.
            session.progress = Progress.model_validate(result.model_dump(include=set(Progress.model_fields)))
            self._persist(session, event_type="EVALUATION_UPDATED",
                          data={"revision": result.revision, "completed": result.completed},
                          evaluation=(str(result.result_id), payload_hash))
            return session

    async def submit_flag(self, sid: str, principal: Principal, submission: FlagSubmission):
        session = self.get(sid, principal)
        self._live(session)
        if session.status != Status.RUNNING or session.operation:
            raise CoreError("SESSION_NOT_RUNNING", "Flag submission requires a running session")
        if submission.generation != session.generation:
            raise CoreError("STALE_ATTEMPT", "Submission belongs to another attempt")
        if submission.flag_id not in {flag.id for flag in session.definition.flags}:
            raise CoreError("UNKNOWN_FLAG", "Unknown flag", 422)
        validator = self.validators.get(session.definition.validator.id) if session.definition.validator else None
        if validator is None:
            raise CoreError("VALIDATOR_UNAVAILABLE", "Validator is unavailable", 503)
        try:
            result = await self._call(validator.submit(context_for(session), submission), self.integration_timeout)
            result = Evaluation.model_validate(result)
        except Exception as exc:
            raise CoreError("VALIDATOR_FAILED", "Validator could not process the submission", 503) from exc
        # Rechecks state + generation after the external call, including stop/restart races.
        return await self.apply_evaluation(sid, result)

    async def access(self, sid: str, principal: Principal) -> AccessGrant:
        session = self.get(sid, principal)
        self._live(session)
        if session.status != Status.RUNNING or session.operation:
            raise CoreError("SESSION_NOT_RUNNING", "Access requires a running session")
        if self.access_broker is None:
            raise CoreError("ACCESS_UNAVAILABLE", "Session access broker is unavailable", 503)
        try:
            grant = AccessGrant.model_validate(await self._call(
                self.access_broker.issue(context_for(session), principal), self.integration_timeout))
            now = self.clock()
            if grant.session_id != session.session_id or grant.generation != session.generation:
                raise ValueError("Incorrect access scope")
            if not now < grant.expires_at <= min(session.expires_at, now + timedelta(minutes=5)):
                raise ValueError("Invalid grant expiry")
            for kind in ("browser", "terminal"):
                url = getattr(grant, f"{kind}_url")
                if getattr(session.definition, kind).required and not url:
                    raise ValueError("Missing required access URL")
                if url:
                    parsed = urlsplit(url)
                    if "\\" in url or any(ord(c) < 33 for c in url):
                        raise ValueError("Invalid access URL")
                    same_origin = url.startswith("/") and not url.startswith("//")
                    secure = parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                    if not (same_origin or secure):
                        raise ValueError("Access URL must be HTTPS or same-origin")
        except Exception as exc:
            raise CoreError("ACCESS_FAILED", "Session access could not be issued", 503) from exc
        current = self.get(sid, principal)
        self._live(current)
        if current.status != Status.RUNNING or current.generation != grant.generation or current.operation:
            raise CoreError("ACCESS_STALE", "Session changed while access was being issued")
        return grant

    async def wait_idle(self, sid: str):
        task = self._jobs.get(str(sid))
        if task:
            await asyncio.shield(task)

    async def close(self):
        self._closing = True
        tasks = list(self._jobs.values())
        for cancel in self._cancels.values():
            cancel.set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.operation_timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Running sessions survive graceful control-plane restarts; TTL is reconciled on boot.
