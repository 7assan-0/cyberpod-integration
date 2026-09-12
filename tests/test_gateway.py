import asyncio
import gzip
from uuid import uuid4
from aiohttp import WSMsgType, web
from aiohttp.test_utils import AioHTTPTestCase, TestServer
from cyberpod_gateway.app import DesktopGateway, create_app

class GatewayTests(AioHTTPTestCase):
    async def get_application(self):
        self.requests = []
        async def echo(request):
            if request.headers.get('Upgrade') == 'websocket':
                ws = web.WebSocketResponse(protocols=('binary',))
                await ws.prepare(request)
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        await ws.send_bytes(message.data)
                return ws
            self.requests.append((dict(request.query), dict(request.headers), await request.read()))
            return web.Response(body=gzip.compress(b'desktop'), headers={'Content-Encoding': 'gzip'})
        target = web.Application()
        target.router.add_route('*', '/{path:.*}', echo)
        self.target = TestServer(target)
        await self.target.start_server()
        self.sid = str(uuid4())
        self.gateway = DesktopGateway()
        self.gateway.register(self.sid, str(self.target.make_url('')).rstrip('/'), 'Password123')
        self.ticket = self.gateway.store.issue(self.sid, 'alice', 1, self.gateway.upstreams[self.sid])
        return create_app(self.gateway, operator_token='private-operator-token-for-tests', subject_for_request=lambda request: request.cookies.get('student', 'alice'))
    async def asyncTearDown(self):
        await super().asyncTearDown()
        await self.target.close()
    async def test_http_preserves_query_and_body_but_strips_control_credentials(self):
        response = await self.client.post(f'/desktop/{self.sid}/echo?t={self.ticket.token}&mode=fit', data=b'payload',
                                          headers={'Authorization': 'Bearer private', 'Cookie': 'cyberpod_session=private'})
        self.assertEqual(await response.text(), 'desktop')
        query, headers, body = self.requests[-1]
        self.assertEqual(query, {'mode': 'fit'})
        self.assertEqual(body, b'payload')
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('Authorization', headers)
    async def test_ticket_scope_owner_credentials_and_revocation(self):
        self.assertEqual((await self.client.get(f'/desktop/{uuid4()}/?t={self.ticket.token}')).status, 403)
        self.assertEqual((await self.client.get(f'/desktop/{self.sid}/?t={self.ticket.token}', headers={'Cookie': 'student=bob'})).status, 403)
        response = await self.client.get(f'/desktop/{self.sid}/credentials?t={self.ticket.token}')
        self.assertEqual((await response.json())['password'], 'Password123')
        self.gateway.revoke(self.sid)
        self.assertEqual((await self.client.get(f'/desktop/{self.sid}/?t={self.ticket.token}')).status, 403)
    async def test_same_origin_grants_honor_secure_cookie_configuration(self):
        self.gateway.secure_cookies = True
        url, _ = self.gateway.issue_url(self.sid, 'alice', 1)
        self.assertTrue(url.startswith(f'/desktop/{self.sid}/'))
        response = await self.client.get(url)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies['cyberpod_desktop']['secure'])
    async def test_open_binary_socket_closes_immediately_on_revoke(self):
        ws = await self.client.ws_connect(f'/desktop/{self.sid}/websockify?t={self.ticket.token}', protocols=('binary',))
        await ws.send_bytes(b'frame')
        self.assertEqual((await ws.receive(timeout=2)).data, b'frame')
        self.gateway.revoke(self.sid)
        message = await ws.receive(timeout=2)
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        await ws.close()
    async def test_old_generation_ticket_and_unknown_operator_rejected(self):
        self.gateway.store.issue(self.sid, 'alice', 2, self.gateway.upstreams[self.sid])
        self.assertEqual((await self.client.get(f'/desktop/{self.sid}/?t={self.ticket.token}')).status, 403)
        self.assertEqual((await self.client.post('/internal/revoke', json={'session_id': self.sid})).status, 401)
