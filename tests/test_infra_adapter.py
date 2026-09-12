import asyncio
import subprocess
from unittest.mock import patch
from cyberpod_core.errors import CoreError
from cyberpod_core.infra_runtime import CliInfraClient, InfraRuntime, MemoryInfraClient, snapshot_from_infra, to_infra_request
from cyberpod_core.models import context_for
from .support import Harness

class InfraAdapterTests(Harness):
    async def test_unknown_inventory_does_not_fabricate_ready_resources(self):
        session = await self.create()
        snapshot = snapshot_from_infra(context_for(session), {'status': 'READY', 'resources': {}})
        self.assertEqual(snapshot.resources, [])
    async def test_stop_then_start_and_restart_use_worker_generation(self):
        self.use_runtime(InfraRuntime(MemoryInfraClient()))
        session = await self.operate(await self.create(), 'start')
        old = {row.provider_id for row in session.resources}
        session = await self.operate(session, 'stop')
        session = await self.operate(session, 'start')
        self.assertEqual(session.status.value, 'RUNNING')
        self.assertTrue(old.isdisjoint({row.provider_id for row in session.resources}))
        session = await self.operate(session, 'restart')
        self.assertEqual(session.status.value, 'RUNNING')
    async def test_live_adapter_rejects_mount_instead_of_silently_rewriting_it(self):
        definition = self.registry.get('hello-lab')
        runtime = InfraRuntime(CliInfraClient())
        with self.assertRaises(CoreError):
            runtime.validate_lab(definition)
        session = await self.create()
        self.assertEqual(to_infra_request(context_for(session))['services'][0]['volumes'][0]['target'], '/scratch')
    async def test_cli_string_error_is_reported_as_core_error(self):
        client = CliInfraClient()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'{"error":"CONTAINER_UNHEALTHY"}', b'')):
            with self.assertRaises(CoreError) as caught:
                client.status('session')
        self.assertEqual(caught.exception.code, 'CONTAINER_UNHEALTHY')
