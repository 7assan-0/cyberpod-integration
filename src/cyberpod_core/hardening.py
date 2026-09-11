from __future__ import annotations
import os, time
from collections import defaultdict, deque
from uuid import uuid4
from aiohttp import web
from .errors import CoreError
from .logging_config import configure_logging

log = __import__('logging').getLogger('cyberpod.audit')
PUBLIC = {'/healthz', '/readyz'}

class RateLimiter:
    def __init__(self, limit=60, window=60.0, auth_limit=10):
        self.limit, self.window, self.auth_limit = limit, window, auth_limit
        self._hits, self._auth = defaultdict(deque), defaultdict(deque)

    def _allow(self, bucket, limit, now):
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def check(self, key, path, now=None):
        now = time.monotonic() if now is None else now
        if not self._allow(self._hits[key], self.limit, now):
            raise CoreError('RATE_LIMITED', 'Too many requests', 429)
        if path.endswith('/login') or path.endswith('/auth/login'):
            if not self._allow(self._auth[key], self.auth_limit, now):
                raise CoreError('RATE_LIMITED', 'Too many authentication attempts', 429)

def client_key(request):
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote or 'unknown')

def request_is_https(request):
    proto = request.headers.get('X-Forwarded-Proto', request.scheme).split(',')[0].strip().lower()
    return proto == 'https'

def apply_hardening(app, *, limiter=None, require_https=None):
    configure_logging()
    limiter = limiter or RateLimiter()
    if require_https is None:
        require_https = os.environ.get('CYBERPOD_REQUIRE_HTTPS', '').lower() in {'1', 'true', 'yes'}
    app['rate_limiter'] = limiter
    app['require_https'] = require_https

    @web.middleware
    async def harden(request, handler):
        started = time.monotonic()
        request_id = request.headers.get('X-Request-ID') or str(uuid4())
        try:
            if require_https and request.path not in PUBLIC and not request_is_https(request):
                raise CoreError('HTTPS_REQUIRED', 'Use HTTPS', 400)
            if request.path not in PUBLIC:
                limiter.check(client_key(request), request.path)
            response = await handler(request)
        except CoreError as exc:
            response = web.json_response({'error': {'code': exc.code, 'message': exc.message, 'request_id': request_id}}, status=exc.status)
            if exc.code == 'RATE_LIMITED':
                response.headers['Retry-After'] = '60'
        response.headers['X-Request-ID'] = request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if require_https:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        log.info('http_request', extra={'request_id': request_id, 'method': request.method, 'route': request.path,
                                        'http_status': response.status, 'client': client_key(request),
                                        'duration_ms': int((time.monotonic() - started) * 1000)})
        return response
    app.middlewares.insert(0, harden)
    return app
