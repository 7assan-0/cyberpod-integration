from __future__ import annotations
import os, time
from collections import defaultdict, deque
from uuid import uuid4
from aiohttp import web
from .errors import CoreError
from .logging_config import configure_logging

log = __import__('logging').getLogger('cyberpod.audit')
PUBLIC = {'/healthz', '/readyz'}
RATE_LIMITER = web.AppKey('rate_limiter', object)
REQUIRE_HTTPS = web.AppKey('require_https', bool)

class RateLimiter:
    def __init__(self, limit=60, window=60.0, auth_limit=10, desktop_limit=300):
        self.limit, self.window, self.auth_limit = limit, window, auth_limit
        self.desktop_limit = desktop_limit
        self._hits, self._auth = defaultdict(deque), defaultdict(deque)
        self._desktop = defaultdict(deque)

    def _allow(self, bucket, limit, now):
        while bucket and now - bucket[0] >= self.window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def check(self, key, path, now=None):
        now = time.monotonic() if now is None else now
        desktop = path.startswith('/desktop/')
        bucket = self._desktop[key] if desktop else self._hits[key]
        limit = self.desktop_limit if desktop else self.limit
        if not self._allow(bucket, limit, now):
            raise CoreError('RATE_LIMITED', 'Too many requests', 429)
        if path.endswith('/login') or path.endswith('/auth/login'):
            if not self._allow(self._auth[key], self.auth_limit, now):
                raise CoreError('RATE_LIMITED', 'Too many authentication attempts', 429)

def client_key(request, trusted_proxies=()):
    forwarded = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
    return forwarded if request.remote in trusted_proxies and forwarded else request.remote or 'unknown'

def request_is_https(request, trusted_proxies=()):
    proto = request.scheme
    if request.remote in trusted_proxies:
        proto = request.headers.get('X-Forwarded-Proto', proto).split(',')[0].strip().lower()
    return proto == 'https'

def apply_hardening(app, *, limiter=None, require_https=None, trusted_proxies=()):
    configure_logging()
    limiter = limiter or RateLimiter()
    if require_https is None:
        require_https = os.environ.get('CYBERPOD_REQUIRE_HTTPS', '').lower() in {'1', 'true', 'yes'}
    app[RATE_LIMITER] = limiter
    app[REQUIRE_HTTPS] = require_https

    @web.middleware
    async def harden(request, handler):
        started = time.monotonic()
        request_id = str(uuid4())
        try:
            if require_https and request.path not in PUBLIC and not request_is_https(request, trusted_proxies):
                raise CoreError('HTTPS_REQUIRED', 'Use HTTPS', 400)
            if request.path not in PUBLIC:
                limiter.check(client_key(request, trusted_proxies), request.path)
            response = await handler(request)
        except CoreError as exc:
            response = web.json_response({'error': {'code': exc.code, 'message': exc.message, 'request_id': request_id}}, status=exc.status)
            if exc.code == 'RATE_LIMITED':
                response.headers['Retry-After'] = str(max(1, int(limiter.window)))
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if require_https and request_is_https(request, trusted_proxies):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        log.info('http_request', extra={'request_id': request_id, 'method': request.method, 'route': request.path,
                                        'http_status': response.status, 'client': client_key(request, trusted_proxies),
                                        'duration_ms': int((time.monotonic() - started) * 1000)})
        return response
    app.middlewares.insert(0, harden)
    return app
