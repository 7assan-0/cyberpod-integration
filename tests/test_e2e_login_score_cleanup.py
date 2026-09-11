from aiohttp.test_utils import TestClient, TestServer
from cyberpod_core.models import Status
from cyberpod_core.session_secrets import SessionVault
from cyberpod_core.student_e2e import ScoreValidator, create_student_app
from .support import ROOT, Harness
import shutil

class EndToEndJourney(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        source = ROOT / 'tests/fixtures/hydra-e2e'
        if not source.exists():
            source = ROOT / 'labs/hydra-e2e'
        shutil.copytree(source, self.path / 'labs/hydra-e2e', dirs_exist_ok=True)
        self.registry.reload()
        self.vault = SessionVault()
        self.engine.validators['e2e-validator'] = ScoreValidator(self.vault)
        self.server = TestServer(create_student_app(self.engine, vault=self.vault))
        await self.server.start_server()
        self.http = TestClient(self.server)
        await self.http.start_server()
    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()
        await super().asyncTearDown()
    async def test_login_wrong_flag_score_cleanup(self):
        denied = await self.http.post('/api/v1/auth/login', json={'email': 'demo@cyberpod.local', 'password': 'nope'})
        self.assertEqual(denied.status, 401)
        login = await self.http.post('/api/v1/auth/login', json={'email': 'demo@cyberpod.local', 'password': 'CyberPodDemo123!'})
        csrf = (await login.json())['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        created = await self.http.post('/api/v1/labs/hydra-e2e/sessions', headers=headers, json={})
        created_body = await created.json()
        sid = created_body['session']['session_id']
        started = await self.http.post(f'/api/v1/sessions/{sid}/start', headers=headers, json={})
        started_body = await started.json()
        self.assertEqual(started_body['session']['status'], 'RUNNING')
        self.assertEqual(started_body['session']['session_id'], sid)
        self.assertNotIn('CYBERPOD{', str(started_body))
        secret = self.vault.target_config(sid, started_body['session']['generation'])
        self.assertIsNotNone(secret)
        wrong = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=headers, json={'flag': 'CYBERPOD{nope}', 'expected_revision': started_body['session']['revision']})
        wrong_body = await wrong.json()
        self.assertEqual(wrong_body['result'], 'INCORRECT')
        right = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=headers, json={'flag': secret['flag'], 'expected_revision': wrong_body['session']['revision']})
        right_body = await right.json()
        self.assertEqual(right_body['result'], 'ACCEPTED')
        self.assertEqual(right_body['session']['score']['earned'], 100)
        self.assertNotIn(secret['flag'], str(right_body))
        cleaned = await self.http.post(f'/api/v1/sessions/{sid}/cleanup', headers=headers, json={})
        self.assertEqual((await cleaned.json())['session']['status'], 'CLEANED')
        self.assertIsNone(self.vault.target_config(sid, started_body['session']['generation']))
        self.assertEqual(self.repo.get(sid).status, Status.CLEANED)
