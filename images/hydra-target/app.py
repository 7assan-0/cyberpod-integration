"""Intentionally weak HTTP Basic authentication, isolated training use only."""
import base64, binascii, hmac, json, os, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from uuid import UUID

class Target(HTTPServer):
    allow_reuse_address = True
    def __init__(self, address, config, emit=None):
        required = {'session_id', 'generation', 'username', 'password', 'flag', 'expires_at'}
        if (not isinstance(config, dict) or set(config) != required
                or type(config['generation']) is not int or config['generation'] < 1):
            raise ValueError('Invalid target configuration')
        if any(not isinstance(config[k], str) or not config[k] for k in ('session_id','username','password','flag')):
            raise ValueError('Invalid secret values')
        try:
            UUID(config['session_id'])
        except ValueError:
            raise ValueError('Invalid session identity') from None
        deadline = config['expires_at']
        if type(deadline) not in (int, float) or not time.time() < deadline < float('inf'):
            raise ValueError('Invalid or expired target deadline')
        self.config = config
        self.emit = emit or (lambda event: print(json.dumps(event), flush=True))
        super().__init__(address, Handler)
    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)
        return sock, address

class Handler(BaseHTTPRequestHandler):
    server_version = 'CyberPodTraining/1'
    sys_version = ''
    def log_message(self, *args):
        pass
    def reply(self, status, body, challenge=False):
        encoded = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if challenge:
            self.send_header('WWW-Authenticate', 'Basic realm="CyberPod Training", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(encoded)
    def observation(self, kind):
        self.server.emit({'schema_version': 1, 'kind': kind})
    def authenticated(self):
        auth = self.headers.get('Authorization', '')
        try:
            scheme, token = auth.split(' ', 1)
            if scheme.lower() != 'basic' or len(token) > 2048:
                return False
            pair = base64.b64decode(token, validate=True)
            expected = (self.server.config['username'] + ':' + self.server.config['password']).encode()
            return hmac.compare_digest(pair, expected)
        except (ValueError, binascii.Error):
            return False
    def do_GET(self):
        if time.time() >= self.server.config['expires_at']:
            return self.reply(410, 'Session expired')
        if self.path == '/health':
            return self.reply(200, 'ready')
        if self.path == '/':
            self.observation('target_reached')
            return self.reply(200, '<h1>Training archive</h1><a href="/login">Staff login</a>')
        if self.path not in ('/login', '/vault'):
            return self.reply(404, 'Not found')
        self.observation('auth_service_reached')
        if not self.authenticated():
            return self.reply(401, 'Authentication required', True)
        self.observation('authentication_success')
        if self.path == '/login':
            return self.reply(200, '<h1>Authenticated</h1><a href="/vault">Open vault</a>')
        return self.reply(200, '<pre>' + self.server.config['flag'] + '</pre>')

if __name__ == '__main__':
    config = json.loads(Path(os.environ.get('TARGET_CONFIG_FILE','/run/secrets/target.json')).read_text())
    with Target((os.environ.get('BIND_HOST','127.0.0.1'), int(os.environ.get('PORT','8080'))), config) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
