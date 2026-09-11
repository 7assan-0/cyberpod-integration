from __future__ import annotations
import asyncio
from urllib.parse import urlsplit
from aiohttp import ClientSession, WSMsgType, web
from .tickets import TicketStore

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host"}

def _ticket_from(request):
    return request.query.get("t") or request.headers.get("X-CyberPod-Ticket") or request.cookies.get("cyberpod_desktop", "")

class DesktopGateway:
    def __init__(self, store=None, *, frame_ancestors="'self'", public_base="http://127.0.0.1:8088"):
        self.store = store or TicketStore()
        self.upstreams = {}
        self.frame_ancestors = frame_ancestors
        self.public_base = public_base.rstrip("/")
        self._client = None

    def register(self, session_id, upstream):
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Upstream must be a loopback HTTP service")
        self.upstreams[session_id] = upstream.rstrip("/")

    def revoke(self, session_id):
        self.upstreams.pop(session_id, None)
        return self.store.revoke_session(session_id)

    def issue_url(self, session_id, subject, generation):
        upstream = self.upstreams.get(session_id)
        if not upstream:
            raise KeyError("SESSION_UPSTREAM")
        ticket = self.store.issue(session_id, subject, generation, upstream)
        return f"{self.public_base}/desktop/{session_id}/?t={ticket.token}", ticket.expires_at

    def authorize(self, request, session_id):
        token = _ticket_from(request)
        if not token:
            raise web.HTTPUnauthorized(text='{"error":{"code":"AUTH_REQUIRED"}}', content_type="application/json")
        try:
            ticket = self.store.resolve(token, session_id)
        except PermissionError as exc:
            payload = f'{{"error":{{"code":"{exc}"}}}}'
            if str(exc) == "TICKET_EXPIRED":
                raise web.HTTPGone(text=payload, content_type="application/json") from exc
            raise web.HTTPForbidden(text=payload, content_type="application/json") from exc
        if session_id not in self.upstreams:
            raise web.HTTPForbidden(text='{"error":{"code":"DESKTOP_REVOKED"}}', content_type="application/json")
        return ticket

    async def client(self):
        if self._client is None or self._client.closed:
            self._client = ClientSession()
        return self._client

    async def close(self):
        if self._client and not self._client.closed:
            await self._client.close()

    def _secure_headers(self):
        return {
            "Content-Security-Policy": f"frame-ancestors {self.frame_ancestors}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        }

    async def page(self, request):
        session_id = request.match_info["session_id"]
        ticket = self.authorize(request, session_id)
        tail = request.match_info.get("path", "")
        if request.headers.get("Upgrade", "").lower() == "websocket" or tail.endswith("websockify"):
            return await self.websocket(request, ticket)
        upstream = f"{ticket.upstream}/{tail}".rstrip("/") or ticket.upstream + "/"
        client = await self.client()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        async with client.request(request.method, upstream, headers=headers, allow_redirects=False) as incoming:
            body = await incoming.read()
            outgoing = web.Response(status=incoming.status, body=body)
            for key, value in incoming.headers.items():
                if key.lower() not in HOP and key.lower() != "content-length":
                    outgoing.headers[key] = value
            outgoing.headers.update(self._secure_headers())
            if tail in {"", "vnc.html", "desktop.html"}:
                outgoing.set_cookie("cyberpod_desktop", ticket.token, httponly=True, samesite="Lax", path=f"/desktop/{session_id}")
            return outgoing

    async def websocket(self, request, ticket=None):
        session_id = request.match_info["session_id"]
        ticket = ticket or self.authorize(request, session_id)
        parsed = urlsplit(ticket.upstream)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        target = f"{scheme}://{parsed.netloc}/websockify"
        browser = web.WebSocketResponse()
        await browser.prepare(request)
        client = await self.client()
        try:
            async with client.ws_connect(target) as upstream:
                async def to_up():
                    async for msg in browser:
                        if msg.type == WSMsgType.TEXT:
                            await upstream.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await upstream.send_bytes(msg.data)
                        elif msg.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                            await upstream.close()
                            break
                async def to_browser():
                    async for msg in upstream:
                        if msg.type == WSMsgType.TEXT:
                            await browser.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await browser.send_bytes(msg.data)
                        elif msg.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                            await browser.close()
                            break
                await asyncio.gather(to_up(), to_browser())
        except Exception:
            if not browser.closed:
                await browser.close()
        return browser

def create_app(gateway=None, operator_token="operator-dev"):
    gateway = gateway or DesktopGateway()
    app = web.Application()
    app["gateway"] = gateway
    app["operator_token"] = operator_token
    def require_operator(request):
        if request.headers.get("Authorization") != f"Bearer {app['operator_token']}":
            raise web.HTTPUnauthorized()
    async def register(request):
        require_operator(request)
        body = await request.json()
        gateway.register(body["session_id"], body["upstream"])
        url, expires = gateway.issue_url(body["session_id"], body["subject"], int(body["generation"]))
        return web.json_response({"desktop_url": url, "expires_at": expires})
    async def revoke(request):
        require_operator(request)
        body = await request.json()
        return web.json_response({"revoked": gateway.revoke(body["session_id"])})
    app.router.add_post("/internal/register", register)
    app.router.add_post("/internal/revoke", revoke)
    app.router.add_route("*", "/desktop/{session_id}/", gateway.page)
    app.router.add_route("*", "/desktop/{session_id}/{path:.*}", gateway.page)
    async def on_close(app):
        await gateway.close()
    app.on_cleanup.append(on_close)
    return app
