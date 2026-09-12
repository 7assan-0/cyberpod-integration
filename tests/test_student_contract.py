import shutil
from datetime import timedelta
from aiohttp.test_utils import TestClient, TestServer
from cyberpod_core.models import Principal
from cyberpod_core.session_secrets import SessionVault
from cyberpod_integration.student_api import create_student_app
from cyberpod_integration.validation import ScoreValidator
from .support import ROOT, Harness

class StudentContractTests(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        shutil.copytree(ROOT / 'labs/hydra-e2e', self.path / 'labs/hydra-e2e')
        self.registry.reload()
        self.vault = SessionVault()
        self.engine.validators['e2e-validator'] = ScoreValidator(self.vault)
        users = {'alice@example.test': ('AlicePassword', Principal(subject='alice')),
                 'bob@example.test': ('BobPassword', Principal(subject='bob'))}
        self.app = create_student_app(self.engine, users=users, vault=self.vault)
        self.http = TestClient(TestServer(self.app))
        await self.http.start_server()
        response = await self.http.post('/api/v1/auth/login', json={'email': 'alice@example.test', 'password': 'AlicePassword'})
        self.auth = await response.json()
        self.headers = {'X-CSRF-Token': self.auth['csrf_token']}
    async def asyncTearDown(self):
        await self.http.close()
        await super().asyncTearDown()
    async def new(self):
        response = await self.http.post('/api/v1/labs/hydra-e2e/sessions', json={}, headers=self.headers)
        self.assertEqual(response.status, 201)
        sid = (await response.json())['session']['session_id']
        response = await self.http.post(f'/api/v1/sessions/{sid}/start', json={}, headers=self.headers)
        self.assertEqual(response.status, 200)
        return (await response.json())['session']
    async def test_all_frontend_routes_and_csrf(self):
        self.assertEqual((await self.http.get('/api/v1/auth/me')).status, 200)
        self.assertEqual((await self.http.get('/api/v1/labs/hydra-e2e')).status, 200)
        denied = await self.http.post('/api/v1/labs/hydra-e2e/sessions', json={})
        self.assertEqual(denied.status, 403)
        session = await self.new()
        listing = await (await self.http.get('/api/v1/sessions')).json()
        self.assertEqual(listing['sessions'][0]['session_id'], session['session_id'])
        status = await (await self.http.get(f"/api/v1/sessions/{session['session_id']}/status")).json()
        self.assertIsInstance(status['session']['score'], dict)
        self.assertNotIn('CYBERPOD{', str(status))
        self.assertEqual(status['session']['generation'], session['generation'])
    async def test_repeated_start_keeps_secret_restart_rotates_and_old_flag_fails(self):
        session = await self.new(); sid = session['session_id']
        old = self.vault.target_config(sid, session['generation'])['flag']
        await self.http.post(f'/api/v1/sessions/{sid}/start', json={}, headers=self.headers)
        self.assertEqual(old, self.vault.target_config(sid, session['generation'])['flag'])
        response = await self.http.post(f'/api/v1/sessions/{sid}/restart', json={}, headers=self.headers)
        session = (await response.json())['session']
        self.assertNotEqual(old, self.vault.target_config(sid, session['generation'])['flag'])
        self.assertEqual(len(self.vault._plain), 1)
        response = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=self.headers,
                                        json={'flag': old, 'expected_revision': session['revision']})
        self.assertEqual((await response.json())['result'], 'INCORRECT')
    async def test_other_student_cannot_observe_stop_or_submit_to_session(self):
        session = await self.new(); sid = session['session_id']
        response = await self.http.post('/api/v1/auth/login', json={'email': 'bob@example.test', 'password': 'BobPassword'})
        headers = {'X-CSRF-Token': (await response.json())['csrf_token']}
        self.assertEqual((await self.http.get(f'/api/v1/sessions/{sid}/status')).status, 404)
        for action in ['stop', 'cleanup']:
            self.assertEqual((await self.http.post(f'/api/v1/sessions/{sid}/{action}', headers=headers, json={})).status, 404)
        self.assertEqual((await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=headers,
                                              json={'flag': 'x', 'expected_revision': session['revision']})).status, 404)
        self.assertEqual((await (await self.http.get('/api/v1/sessions')).json())['sessions'], [])
    async def test_bad_requests_stale_revision_and_expiry(self):
        for data in [[], {'email': 1, 'password': []}]:
            response = await self.http.post('/api/v1/auth/login', json=data)
            self.assertIn(response.status, [401, 422])
        session = await self.new(); sid = session['session_id']
        response = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=self.headers,
                                       json={'flag': 'bad', 'expected_revision': -1})
        self.assertEqual(response.status, 409)
        self.now += timedelta(minutes=11)
        status = await (await self.http.get(f'/api/v1/sessions/{sid}/status')).json()
        self.assertFalse(status['session']['flag']['can_submit'])
        response = await self.http.post(f'/api/v1/sessions/{sid}/flags', headers=self.headers,
                                       json={'flag': 'bad', 'expected_revision': session['revision']})
        self.assertEqual(response.status, 410)
    async def test_logout_cleans_owned_sessions_and_revokes_cookie(self):
        session = await self.new()
        response = await self.http.post('/api/v1/auth/logout', headers=self.headers, json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.repo.get(session['session_id']).status.value, 'CLEANED')
        self.assertEqual(self.vault._plain, {})
        self.assertEqual((await self.http.get('/api/v1/auth/me')).status, 401)
