import json, logging, unittest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase
from cyberpod_core.hardening import RateLimiter, apply_hardening
from cyberpod_core.logging_config import JSONFormatter

class HardeningTests(AioHTTPTestCase):
    async def get_application(self):
        async def ok(_):
            return web.json_response({'ok': True})
        async def login(_):
            return web.json_response({'token': 'x'})
        app = web.Application()
        app.router.add_get('/healthz', ok)
        app.router.add_get('/labs', ok)
        app.router.add_post('/api/v1/auth/login', login)
        apply_hardening(app, limiter=RateLimiter(limit=5, window=60, auth_limit=2), require_https=True, trusted_proxies={'127.0.0.1'})
        return app

    async def test_http_api_rejected_when_https_required(self):
        response = await self.client.get('/labs')
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())['error']['code'], 'HTTPS_REQUIRED')

    async def test_https_forwarded_proto_sets_hsts(self):
        response = await self.client.get('/labs', headers={'X-Forwarded-Proto': 'https'})
        self.assertEqual(response.status, 200)
        self.assertIn('max-age', response.headers.get('Strict-Transport-Security', ''))

    async def test_rate_limit_returns_429(self):
        headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '203.0.113.9'}
        last = None
        for _ in range(6):
            last = await self.client.get('/labs', headers=headers)
        self.assertEqual(last.status, 429)
        self.assertEqual(last.headers.get('Retry-After'), '60')

class AuditLogTests(unittest.TestCase):
    def test_json_log_has_no_secrets(self):
        record = logging.LogRecord('cyberpod.audit', logging.INFO, '', 0, 'http_request', (), None)
        record.client = '203.0.113.9'
        record.duration_ms = 12
        payload = json.loads(JSONFormatter().format(record))
        self.assertEqual(payload['client'], '203.0.113.9')
        self.assertNotIn('token', payload)
