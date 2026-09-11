from aiohttp.test_utils import TestClient, TestServer
from cyberpod_core.models import Status
from cyberpod_core.student_e2e import FLAG, ScoreValidator, create_student_app
from .support import Harness

class EndToEndJourney(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.engine.validators['e2e-validator'] = ScoreValidator()
        self.server = TestServer(create_student_app(self.engine))
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
        labs = await self.http.get('/api/v1/labs')
        self.assertIn('hydra-e2e', {item['id'] for item in (await labs.json())['labs']})
        created = await self.http.post('/api/v1/labs/hydra-e2e/sessions', headers=headers, json={})
        sid = (await created.json())['session']['id']
        started = await self.http.post(f'/api/v1/sessions/{sid}/start', headers=headers, json={})
        started_body = await started.json()
        self.assertEqual(started_body['session']['status'], 'RUNNING')
        wrong = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=headers, json={'flag': 'CYBERPOD{nope}', 'expected_revision': started_body['session']['revision']})
        wrong_body = await wrong.json()
        self.assertEqual(wrong_body['flag'], 'INCORRECT')
        right = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=headers, json={'flag': FLAG, 'expected_revision': wrong_body['session']['revision']})
        right_body = await right.json()
        self.assertEqual(right_body['flag'], 'ACCEPTED')
        self.assertEqual(right_body['session']['score'], 100)
        cleaned = await self.http.post(f'/api/v1/sessions/{sid}/cleanup', headers=headers, json={})
        self.assertEqual((await cleaned.json())['session']['status'], 'CLEANED')
        self.assertEqual(self.repo.get(sid).resources, [])
        self.assertEqual(self.runtime.resources, {})
