from uuid import uuid4
from cyberpod_core.cleanup import SessionReaper
from cyberpod_core.models import ResourceRef, Status, context_for
from .support import ALICE, BOB, Harness

class FakeOccupancy:
    def __init__(self):
        self.tickets = {}
        self.revoked = []
    def issue(self, session_id, owner='student-a'):
        token = 't-' + session_id[:8]
        self.tickets[token] = (owner, session_id, 1, 9e18)
        return token
    def revoke(self, session_id):
        drop = [token for token, record in self.tickets.items() if record[1] == session_id]
        for token in drop:
            self.tickets.pop(token, None)
        self.revoked.append(session_id)
        return len(drop)

class FakeInfra:
    def __init__(self):
        self.sessions = {}
    def remember(self, session_id, status='READY'):
        filled = status != 'CLEANED'
        self.sessions[session_id] = {
            'status': status,
            'resources': {
                'network': ['net-x'] if filled else [],
                'volume': ['vol-x'] if filled else [],
                'container': ['ctr-x'] if filled else [],
            },
        }
    def stop(self, session_id, generation=None):
        self.remember(session_id, 'CLEANED')

class SessionReaperTests(Harness):
    async def test_cleanup_leaves_no_session_runtime_or_ticket(self):
        occupancy, infra = FakeOccupancy(), FakeInfra()
        session = await self.operate(await self.create(), 'start')
        occupancy.issue(str(session.session_id))
        infra.remember(str(session.session_id), 'READY')
        cleaned = await SessionReaper(self.engine, occupancy=occupancy, infra=infra).cleanup(str(session.session_id), ALICE)
        self.assertEqual(cleaned.status, Status.CLEANED)
        self.assertEqual(cleaned.resources, [])
        snapshot = await self.runtime.inspect(context_for(cleaned))
        self.assertEqual(snapshot.resources, [])
        self.assertEqual(snapshot.health, 'STOPPED')
        self.assertEqual(self.runtime.resources, {})
        self.assertEqual(occupancy.tickets, {})

    async def test_cleanup_alice_does_not_drop_bob_resources(self):
        alice = await self.operate(await self.create(ALICE), 'start', ALICE)
        bob = await self.operate(await self.create(BOB), 'start', BOB)
        occupancy = FakeOccupancy()
        occupancy.issue(str(alice.session_id), 'student-a')
        occupancy.issue(str(bob.session_id), 'student-b')
        await SessionReaper(self.engine, occupancy=occupancy).cleanup(str(alice.session_id), ALICE)
        self.assertEqual(self.repo.get(str(bob.session_id)).status, Status.RUNNING)
        self.assertTrue(self.repo.get(str(bob.session_id)).resources)

    async def test_auditor_detects_orphan_runtime_record(self):
        session = await self.operate(await self.operate(await self.create(), 'start'), 'cleanup')
        orphan = ResourceRef(provider_id=str(uuid4()), kind='container', logical_id='greeting',
            labels={'cyberpod.managed': 'true', 'cyberpod.deployment': str(session.deployment_id),
                    'cyberpod.session': str(session.session_id), 'cyberpod.generation': str(session.generation)})
        self.runtime.resources[orphan.provider_id] = orphan
        leftovers = await SessionReaper(self.engine).audit(session)
        self.assertIn('runtime.store', leftovers)
        dirty = await SessionReaper(self.engine).sweep()
        self.assertIn(str(session.session_id), dirty)
