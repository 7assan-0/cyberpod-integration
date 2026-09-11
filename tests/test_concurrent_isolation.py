import asyncio, unittest
from uuid import uuid4
from cyberpod_core.errors import CoreError
from cyberpod_core.infra_runtime import InfraRuntime, MemoryInfraClient
from cyberpod_core.isolation import Occupancy
from cyberpod_core.models import FlagSubmission, Status, context_for
from .support import ALICE, BOB, ADMIN, Harness

class ConcurrentIsolationTests(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = MemoryInfraClient()
        self.use_runtime(InfraRuntime(self.client))
        self.occupancy = Occupancy()

    async def test_two_students_start_at_once_and_stay_isolated(self):
        created_a, created_b = await asyncio.gather(self.create(ALICE), self.create(BOB))
        started_a, started_b = await asyncio.gather(
            self.operate(created_a, 'start', ALICE),
            self.operate(created_b, 'start', BOB),
        )
        self.assertEqual(started_a.status, Status.RUNNING)
        self.assertEqual(started_b.status, Status.RUNNING)
        self.assertTrue({r.provider_id for r in started_a.resources}.isdisjoint(
            {r.provider_id for r in started_b.resources}))
        self.assertNotEqual(context_for(started_a).namespace, context_for(started_b).namespace)
        with self.assertRaises(CoreError) as hidden:
            self.engine.get(str(started_a.session_id), BOB)
        self.assertEqual(hidden.exception.code, 'SESSION_NOT_FOUND')
        with self.assertRaises(CoreError):
            await self.engine.command(str(started_a.session_id), 'stop', BOB)
        with self.assertRaises(CoreError):
            await self.engine.submit_flag(str(started_a.session_id), BOB,
                FlagSubmission(submission_id=uuid4(), generation=started_a.generation, flag_id='hello', value='x'))
        ticket_a = self.occupancy.issue_ticket(started_a, ALICE, started_a.generation)
        with self.assertRaises(CoreError):
            self.occupancy.resolve_ticket(ticket_a, str(started_b.session_id), BOB)
        await self.operate(started_a, 'cleanup', ALICE)
        self.occupancy.revoke(str(started_a.session_id))
        self.assertEqual(self.repo.get(str(started_b.session_id)).status, Status.RUNNING)
        self.assertEqual(self.client.sessions[str(started_b.session_id)]['status'], 'READY')
