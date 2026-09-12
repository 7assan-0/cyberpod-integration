import asyncio
import hashlib
import shutil
from datetime import timedelta
from uuid import uuid4

from cyberpod_core.demo_runtime import MemoryRuntime
from cyberpod_core.engine import Engine
from cyberpod_core.errors import CoreError
from cyberpod_core.models import CreateSession, Operation, ResourceRef, RuntimeSnapshot, Status, context_for
from cyberpod_core.repository import SQLiteRepository

from .support import ALICE, BOB, ROOT, ExplodingRuntime, FailingCleanupRuntime, Harness, PausingRuntime


class CoreTests(Harness):
    async def test_mandatory_hello_lifecycle_and_configuration_only_second_lab(self):
        source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (ROOT / "src").rglob("*.py")}
        self.assertEqual([lab.id for lab in self.registry.all()], ["hello-lab"])
        first = await self.create()
        self.assertEqual(first.definition.containers[0].id, "greeting")
        self.assertEqual(first.status, Status.CREATED)
        first = await self.operate(first, "start")
        self.assertEqual(first.status, Status.RUNNING)
        self.assertEqual({r.kind for r in first.resources}, {"container", "network", "volume"})
        first = await self.operate(first, "stop")
        self.assertEqual(first.status, Status.STOPPED)
        self.assertTrue(first.resources)
        first = await self.operate(first, "cleanup")
        self.assertEqual(first.status, Status.CLEANED)
        self.assertEqual(self.runtime.resources, {})
        shutil.copytree(ROOT / "tests/fixtures/second-lab", self.path / "labs/second-lab")
        self.assertEqual(self.registry.reload(), 2)
        second = await self.create(lab="second-lab")
        second = await self.operate(second, "start")
        self.assertEqual(second.status, Status.RUNNING)
        second = await self.operate(second, "stop")
        self.assertEqual(second.status, Status.STOPPED)
        second = await self.operate(second, "cleanup")
        self.assertEqual(second.status, Status.CLEANED)
        self.assertEqual(self.runtime.resources, {})
        self.assertEqual(source_hashes, {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (ROOT / "src").rglob("*.py")})

    async def test_two_sessions_have_distinct_resource_ownership_and_cleanup(self):
        a, b = await self.create(), await self.create(BOB)
        a = await self.operate(a, "start")
        b = await self.operate(b, "start", BOB)
        self.assertTrue({r.provider_id for r in a.resources}.isdisjoint({r.provider_id for r in b.resources}))
        self.assertNotEqual(context_for(a).namespace, context_for(b).namespace)
        self.assertNotEqual(context_for(a).labels, context_for(b).labels)
        await self.operate(a, "cleanup")
        b_inventory = await self.runtime.inspect(context_for(b))
        self.assertEqual(b_inventory.health, "READY")
        self.assertEqual(len(b_inventory.resources), 3)

    async def test_start_stop_and_cleanup_are_idempotent(self):
        s = await self.create()
        a, b = await asyncio.gather(self.engine.command(str(s.session_id), "start", ALICE),
                                    self.engine.command(str(s.session_id), "start", ALICE))
        self.assertEqual(a.operation.operation_id, b.operation.operation_id)
        await self.engine.wait_idle(str(s.session_id))
        running = self.repo.get(str(s.session_id))
        self.assertEqual(len(self.runtime.resources), 3)
        self.assertEqual((await self.engine.command(str(s.session_id), "start", ALICE)).revision, running.revision)
        stopped = await self.operate(s, "stop")
        self.assertEqual((await self.engine.command(str(s.session_id), "stop", ALICE)).revision, stopped.revision)
        cleaned = await self.operate(s, "cleanup")
        self.assertEqual((await self.engine.command(str(s.session_id), "cleanup", ALICE)).revision, cleaned.revision)

    async def test_create_idempotency_is_owner_scoped_and_durable(self):
        a = await self.create(key="retry-key")
        again = await self.create(key="retry-key")
        b = await self.create(BOB, key="retry-key")
        self.assertEqual(a.session_id, again.session_id)
        self.assertNotEqual(a.session_id, b.session_id)
        with self.assertRaises(CoreError) as error:
            await self.engine.create("hello-lab", ALICE, CreateSession(ttl_seconds=60), "retry-key")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        await self.engine.close()
        self.repo.close()
        self.repo = SQLiteRepository(self.path / "core.sqlite3")
        self.engine = Engine(self.registry, self.repo, self.runtime, clock=lambda: self.now)
        self.assertEqual((await self.create(key="retry-key")).session_id, a.session_id)

    async def test_ownership_check_applies_before_any_command(self):
        a = await self.create()
        for action in ("start", "stop", "restart", "cleanup"):
            with self.assertRaises(CoreError) as error:
                await self.engine.command(str(a.session_id), action, BOB)
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.runtime.resources, {})

    async def test_stop_during_start_is_durable_and_does_not_report_running(self):
        self.use_runtime(PausingRuntime())
        s = await self.create()
        await self.engine.command(str(s.session_id), "start", ALICE)
        await self.runtime.entered.wait()
        stopping = await self.engine.command(str(s.session_id), "stop", ALICE)
        self.assertEqual(stopping.status, Status.STOPPING)
        self.runtime.release.set()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.STOPPED)
        self.assertNotIn("SESSION_RUNNING", [e.type for e in self.repo.events(0, 200)])

    async def test_cleanup_during_start_removes_partial_resources(self):
        self.use_runtime(PausingRuntime())
        s = await self.create()
        await self.engine.command(str(s.session_id), "start", ALICE)
        await self.runtime.entered.wait()
        await self.engine.command(str(s.session_id), "cleanup", ALICE)
        self.runtime.release.set()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)
        self.assertEqual(self.runtime.resources, {})

    async def test_partial_start_failure_is_compensated_and_sanitized(self):
        self.use_runtime(ExplodingRuntime())
        s = await self.operate(await self.create(), "start")
        self.assertEqual(s.status, Status.ERROR)
        self.assertEqual(s.resources, [])
        self.assertEqual(self.runtime.resources, {})
        self.assertNotIn("SECRET", s.model_dump_json())

    async def test_unreported_allocation_is_discovered_and_cleaned(self):
        self.use_runtime(ExplodingRuntime(report_resource=False))
        s = await self.operate(await self.create(), "start")
        self.assertEqual(s.status, Status.ERROR)
        self.assertEqual(self.runtime.resources, {})

    async def test_cleanup_failure_preserves_retryable_state_and_other_session(self):
        self.use_runtime(FailingCleanupRuntime())
        a, b = await self.create(), await self.create(BOB)
        a, b = await self.operate(a, "start"), await self.operate(b, "start", BOB)
        a = await self.operate(a, "cleanup")
        self.assertEqual(a.status, Status.ERROR)
        self.assertTrue(a.cleanup_requested)
        self.assertTrue(a.resources)
        self.assertEqual(len((await self.runtime.inspect(context_for(b))).resources), 3)
        self.runtime.fail = False
        await self.engine.sweep_once()
        await self.engine.wait_idle(str(a.session_id))
        self.assertEqual(self.repo.get(str(a.session_id)).status, Status.CLEANED)

    async def test_false_cleanup_success_is_rejected(self):
        class NoCleanup(MemoryRuntime):
            async def cleanup(self, context):
                pass
        self.use_runtime(NoCleanup())
        s = await self.operate(await self.create(), "start")
        s = await self.operate(s, "cleanup")
        self.assertEqual(s.status, Status.ERROR)
        self.assertTrue(s.cleanup_requested)

    async def test_startup_timeout_cleans_allocations(self):
        self.use_runtime(PausingRuntime())
        self.engine.startup_timeout = 0.02
        s = await self.operate(await self.create(), "start")
        self.assertEqual(s.error.code, "RUNTIME_TIMEOUT")
        self.assertEqual(self.runtime.resources, {})

    async def test_ready_requires_manifest_resources(self):
        class MissingResources(MemoryRuntime):
            async def start(self, context, cancel, report):
                return RuntimeSnapshot(health="READY")
        self.use_runtime(MissingResources())
        self.assertEqual((await self.operate(await self.create(), "start")).status, Status.ERROR)

    async def test_stop_requires_provider_confirmation(self):
        class NoStop(MemoryRuntime):
            async def stop(self, context):
                pass
        self.use_runtime(NoStop())
        s = await self.operate(await self.create(), "start")
        self.assertEqual((await self.operate(s, "stop")).status, Status.ERROR)

    async def test_restart_replaces_resources_increments_generation_and_preserves_expiry(self):
        s = await self.operate(await self.create(), "start")
        old_ids = {r.provider_id for r in s.resources}
        expiry = s.expires_at
        s = await self.operate(s, "restart")
        self.assertEqual(s.generation, 2)
        self.assertEqual(s.status, Status.RUNNING)
        self.assertEqual(s.expires_at, expiry)
        self.assertTrue(old_ids.isdisjoint(self.runtime.resources))
        self.assertTrue(all(r.labels["cyberpod.generation"] == "2" for r in s.resources))

    async def test_start_after_stop_preserves_attempt_and_resource_ids(self):
        s = await self.operate(await self.create(), "start")
        ids = {r.provider_id for r in s.resources}
        s = await self.operate(s, "stop")
        s = await self.operate(s, "start")
        self.assertEqual(s.generation, 1)
        self.assertEqual(ids, {r.provider_id for r in s.resources})

    async def test_cleaned_is_terminal(self):
        s = await self.operate(await self.create(), "cleanup")
        for action in ("start", "restart"):
            with self.assertRaises(CoreError) as error:
                await self.engine.command(str(s.session_id), action, ALICE)
            self.assertEqual(error.exception.code, "SESSION_CLEANED")

    async def test_expiry_prevents_start_and_sweeper_cleans_created_session(self):
        s = await self.create()
        self.now += timedelta(seconds=601)
        with self.assertRaises(CoreError) as error:
            await self.engine.command(str(s.session_id), "start", ALICE)
        self.assertEqual(error.exception.status, 410)
        await self.engine.sweep_once()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)

    async def test_expiry_during_start_cleans_without_becoming_running(self):
        self.use_runtime(PausingRuntime())
        s = await self.create()
        await self.engine.command(str(s.session_id), "start", ALICE)
        await self.runtime.entered.wait()
        self.now += timedelta(seconds=601)
        self.runtime.release.set()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)

    async def test_expired_running_session_is_cleaned(self):
        s = await self.operate(await self.create(), "start")
        self.now += timedelta(seconds=601)
        await self.engine.sweep_once()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)

    async def test_quota_is_atomic_for_concurrent_creates(self):
        results = await asyncio.gather(*(self.create() for _ in range(5)), return_exceptions=True)
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 3)
        self.assertEqual(sum(isinstance(r, CoreError) and r.code == "SESSION_QUOTA" for r in results), 2)

    async def test_manifest_removal_and_change_do_not_break_existing_session(self):
        s = await self.create()
        self.edit_manifest(lambda d: d.update(name="Changed catalog name"))
        self.assertEqual(self.repo.get(str(s.session_id)).definition.name, "Hello Lab")
        shutil.rmtree(self.path / "labs/hello-lab")
        self.assertEqual(self.registry.reload(), 0)
        s = await self.operate(s, "start")
        self.assertEqual(s.status, Status.RUNNING)
        self.assertEqual((await self.operate(s, "cleanup")).status, Status.CLEANED)

    async def test_database_lease_rejects_second_control_process(self):
        with self.assertRaises(CoreError) as error:
            SQLiteRepository(self.path / "core.sqlite3")
        self.assertEqual(error.exception.code, "DATABASE_IN_USE")

    async def test_repository_cas_prevents_lost_updates(self):
        a = await self.create()
        b = self.repo.get(str(a.session_id))
        self.repo.save(a)
        with self.assertRaises(CoreError) as error:
            self.repo.save(b)
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    async def test_transactional_event_cursor(self):
        s = await self.operate(await self.create(), "start")
        events = self.repo.events(0, 2, str(s.session_id))
        self.assertEqual([e.type for e in events], ["SESSION_CREATED", "SESSION_STARTING"])
        more = self.repo.events(events[-1].sequence, 100, str(s.session_id))
        self.assertEqual([e.type for e in more], ["SESSION_RUNNING"])

    async def test_crash_recovery_cleans_interrupted_start_using_provider_labels(self):
        s = await self.create()
        resource = ResourceRef(provider_id=str(uuid4()), kind="network", logical_id="training", labels=context_for(s).labels)
        self.runtime.resources[resource.provider_id] = resource  # not reported before simulated crash
        s.status = Status.STARTING
        s.operation = Operation(operation_id=uuid4(), action="start", started_at=self.now)
        self.repo.save(s)
        await self.engine.reconcile()
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)
        self.assertEqual(self.runtime.resources, {})

    async def test_persisted_running_session_is_verified_after_reopen(self):
        s = await self.operate(await self.create(), "start")
        deployment_id = self.repo.deployment_id
        await self.engine.close()
        self.repo.close()
        self.repo = SQLiteRepository(self.path / "core.sqlite3")
        self.engine = Engine(self.registry, self.repo, self.runtime, clock=lambda: self.now)
        await self.engine.reconcile()
        restored = self.repo.get(str(s.session_id))
        self.assertEqual(restored.status, Status.RUNNING)
        self.assertEqual(self.repo.deployment_id, deployment_id)
        self.assertEqual(len(restored.resources), 3)

    async def test_background_health_failure_revokes_state_and_triggers_cleanup(self):
        s = await self.operate(await self.create(), "start")
        self.runtime.health[(str(s.session_id), s.generation)] = "FAILED"
        await self.engine.sweep_once()
        failed = self.repo.get(str(s.session_id))
        self.assertEqual(failed.status, Status.ERROR)
        self.assertEqual(failed.error.code, "RUNTIME_UNHEALTHY")
        await self.engine.sweep_once()
        await self.engine.wait_idle(str(s.session_id))
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.CLEANED)

    async def test_foreign_resource_reports_are_rejected(self):
        class ForeignRuntime(MemoryRuntime):
            async def start(self, context, cancel, report):
                resource = ResourceRef(provider_id="foreign", kind="network", logical_id="training",
                                       labels={**context.labels, "cyberpod.session": str(uuid4())})
                self.resources[resource.provider_id] = resource
                await report(resource)
        self.use_runtime(ForeignRuntime())
        s = await self.operate(await self.create(), "start")
        self.assertEqual(s.status, Status.ERROR)
        self.assertIn("foreign", self.runtime.resources)  # never destroy foreign ownership
        self.assertEqual(s.resources, [])

    async def test_runtime_switch_does_not_destroy_resources_using_wrong_adapter(self):
        s = await self.operate(await self.create(), "start")
        self.engine.runtime.name = "other-runtime"
        with self.assertRaises(CoreError) as error:
            await self.engine.command(str(s.session_id), "cleanup", ALICE)
        self.assertEqual(error.exception.code, "RUNTIME_MISMATCH")
        self.assertEqual(len(self.runtime.resources), 3)

