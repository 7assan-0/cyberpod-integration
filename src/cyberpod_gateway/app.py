from __future__ import annotations
import asyncio
import hmac
import os
from contextlib import suppress
from urllib.parse import urlsplit
from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from .tickets import TicketStore

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
PRIVATE = {'authorization', 'cookie', 'x-cyberpod-ticket'}
GATEWAY = web.AppKey('gateway', object)


def _ticket_from(request):
    return request.query.get('t') or request.headers.get('X-CyberPod-Ticket') or request.cookies.get('cyberpod_desktop', '')


class DesktopGateway:
    def __init__(self, store=None, *, frame_ancestors="'self'", public_base='http://127.0.0.1:8088'):
        self.store = store or TicketStore()
        self.upstreams = {}
        self.frame_ancestors = frame_ancestors
        self.public_base = public_base.rstrip('/')
        self._client = None
        self._connections = {}
        self._closing = set()
        self._credentials = {}
        self.subject_for_request = None

    def register(self, session_id, upstream, password=None):
        parsed = urlsplit(upstream)
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Upstream must be a loopback HTTP service without credentials or query')
        self.revoke(session_id)
        self.upstreams[session_id] = upstream.rstrip('/')
        if password is not None:
            if not isinstance(password, str) or not 8 <= len(password) <= 128 or not all('!' <= ch <= '~' for ch in password):
                self.upstreams.pop(session_id, None)
                raise ValueError('Invalid desktop credential')
            self._credentials[session_id] = password

    def revoke(self, session_id):
        self.upstreams.pop(session_id, None)
        self._credentials.pop(session_id, None)
        for connection in list(self._connections.get(session_id, ())):
            task = asyncio.create_task(connection.close(code=1008, message=b'Session revoked'))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        return self.store.revoke_session(session_id)

    def issue_url(self, session_id, subject, generation, expires_at=None):
        upstream = self.upstreams.get(session_id)
        if not upstream:
            raise KeyError('SESSION_UPSTREAM')
        ticket = self.store.issue(session_id, subject, generation, upstream, expires_at=expires_at)
        return f'{self.public_base}/desktop/{session_id}/desktop.html?t={ticket.token}', ticket.expires_at

    def authorize(self, request, session_id):
        token = _ticket_from(request)
        if not token:
            raise web.HTTPUnauthorized(text='{"error":{"code":"AUTH_REQUIRED"}}', content_type='application/json')
        try:
            ticket = self.store.resolve(token, session_id)
        except PermissionError as exc:
            cls = web.HTTPGone if str(exc) == 'TICKET_EXPIRED' else web.HTTPForbidden
            raise cls(text='{"error":{"code":"' + str(exc) + '"}}', content_type='application/json') from exc
        if self.upstreams.get(session_id) != ticket.upstream:
            raise web.HTTPForbidden(text='{"error":{"code":"DESKTOP_REVOKED"}}', content_type='application/json')
        if self.subject_for_request is None:
            raise web.HTTPServiceUnavailable(text='Desktop identity adapter is not configured')
        if self.subject_for_request(request) != ticket.subject:
            raise web.HTTPForbidden(text='{"error":{"code":"TICKET_OWNER"}}', content_type='application/json')
        return ticket

    async def client(self):
        if self._client is None or self._client.closed:
            self._client = ClientSession(auto_decompress=False, timeout=ClientTimeout(total=30))
        return self._client

    async def close(self):
        for sid in list(self.upstreams):
            self.revoke(sid)
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        if self._client and not self._client.closed:
            await self._client.close()

    def _secure_headers(self):
        return {'Content-Security-Policy': f'frame-ancestors {self.frame_ancestors}',
                'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}

    async def page(self, request):
        session_id = request.match_info['session_id']
        ticket = self.authorize(request, session_id)
        tail = request.match_info.get('path', '')
        if any(part in {'.', '..'} for part in tail.split('/')) or '\\' in tail:
            raise web.HTTPBadRequest()
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.websocket(request, ticket)
        if tail == 'credentials':
            if request.method != 'GET':
                raise web.HTTPMethodNotAllowed(request.method, ['GET'])
            password = self._credentials.get(session_id)
            if not password:
                raise web.HTTPServiceUnavailable(text='Desktop credential has not been provisioned')
            return web.json_response({'password': password}, headers=self._secure_headers())
        target = f'{ticket.upstream}/{tail}'
        params = [(key, value) for key, value in request.query.items() if key != 't']
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP | PRIVATE}
        client = await self.client()
        try:
            async with client.request(request.method, target, params=params, data=await request.read(), headers=headers, allow_redirects=False) as incoming:
                outgoing = web.Response(status=incoming.status, body=await incoming.read())
                for key, value in incoming.headers.items():
                    if key.lower() not in HOP | {'set-cookie'}:
                        outgoing.headers[key] = value
                location = outgoing.headers.get('Location')
                if location and (location.startswith(ticket.upstream + '/') or location.startswith('/')):
                    relative = location.removeprefix(ticket.upstream).lstrip('/')
                    outgoing.headers['Location'] = f'/desktop/{session_id}/{relative}'
                outgoing.headers.update(self._secure_headers())
                if tail in {'', 'vnc.html', 'desktop.html'}:
                    outgoing.set_cookie('cyberpod_desktop', ticket.token, httponly=True, secure=self.public_base.startswith('https://'),
                                        samesite='Lax', path=f'/desktop/{session_id}/')
                return outgoing
        except (ClientError, TimeoutError):
            raise web.HTTPBadGateway(text='Desktop upstream is unavailable') from None

    async def websocket(self, request, ticket=None):
        session_id = request.match_info['session_id']
        ticket = ticket or self.authorize(request, session_id)
        parsed = urlsplit(ticket.upstream)
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        target = f'{scheme}://{parsed.netloc}{parsed.path}/websockify'
        browser = web.WebSocketResponse(protocols=('binary',))
        client = await self.client()
        try:
            upstream = await client.ws_connect(target, protocols=('binary',))
        except (ClientError, TimeoutError):
            raise web.HTTPBadGateway(text='Desktop socket is unavailable') from None
        await browser.prepare(request)
        self._connections.setdefault(session_id, set()).add(browser)

        async def relay(source, destination):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await destination.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await destination.send_bytes(message.data)

        async def expiry():
            while not browser.closed:
                await asyncio.sleep(.5)
                self.authorize(request, session_id)

        jobs = [asyncio.create_task(relay(browser, upstream)), asyncio.create_task(relay(upstream, browser)), asyncio.create_task(expiry())]
        try:
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await upstream.close()
            await browser.close()
            self._connections[session_id].discard(browser)
            if not self._connections[session_id]:
                self._connections.pop(session_id, None)
        return browser


def create_app(gateway=None, operator_token=None, *, subject_for_request=None):
    gateway = gateway or DesktopGateway()
    if subject_for_request is not None:
        gateway.subject_for_request = subject_for_request
    operator_token = operator_token or os.environ.get('CYBERPOD_GATEWAY_OPERATOR_TOKEN')
    if not isinstance(operator_token, str) or len(operator_token) < 24:
        raise ValueError('Configure a private gateway operator token of at least 24 characters')
    app = web.Application(client_max_size=16384)
    app[GATEWAY] = gateway

    async def operator_body(request):
        if not hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {operator_token}'):
            raise web.HTTPUnauthorized()
        try:
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None

    async def register(request):
        body = await operator_body(request)
        try:
            from uuid import UUID
            session_id = str(UUID(body['session_id']))
            generation = body['generation']
            if type(generation) is not int or generation < 1 or not isinstance(body['subject'], str) or not body['subject']:
                raise ValueError()
            gateway.register(session_id, body['upstream'], body.get('password'))
            url, expires = gateway.issue_url(session_id, body['subject'], generation)
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        return web.json_response({'desktop_url': url, 'expires_at': expires})

    async def revoke(request):
        body = await operator_body(request)
        if not isinstance(body.get('session_id'), str):
            raise web.HTTPBadRequest()
        return web.json_response({'revoked': gateway.revoke(body['session_id'])})

    app.router.add_post('/internal/register', register)
    app.router.add_post('/internal/revoke', revoke)
    app.router.add_route('*', '/desktop/{session_id}/', gateway.page)
    app.router.add_route('*', '/desktop/{session_id}/{path:.*}', gateway.page)
    async def on_close(_):
        await gateway.close()
    app.on_cleanup.append(on_close)
    return app
