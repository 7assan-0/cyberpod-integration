import base64, binascii, hmac, json, os, time
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from uuid import UUID

class Target(HTTPServer):
    allow_reuse_address = True
    def __init__(self, address, config):
        self.config = config
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    server_version = 'CyberPodTraining/1'
    sys_version = ''
    def log_message(self, *args):
        return
    def reply(self, status, body, challenge=False):
        encoded = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        if challenge:
            self.send_header('WWW-Authenticate', 'Basic realm="CyberPod Training"')
        self.end_headers()
        self.wfile.write(encoded)
    def authenticated(self):
        auth = self.headers.get('Authorization', '')
        try:
            scheme, token = auth.split(' ', 1)
            pair = base64.b64decode(token, validate=True)
            expected = (self.server.config['username'] + ':' + self.server.config['password']).encode()
            return scheme.lower() == 'basic' and hmac.compare_digest(pair, expected)
        except (ValueError, binascii.Error):
            return False
    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, 'ready')
        if self.path == '/':
            return self.reply(200, '<h1>Training archive</h1>')
        if self.path not in ('/login', '/vault'):
            return self.reply(404, 'Not found')
        if not self.authenticated():
            return self.reply(401, 'Authentication required', True)
        if self.path == '/login':
            return self.reply(200, '<h1>Authenticated</h1>')
        return self.reply(200, '<pre>' + self.server.config['flag'] + '</pre>')

if __name__ == '__main__':
    config = json.loads(Path(os.environ.get('TARGET_CONFIG_FILE', '/app/target.json')).read_text())
    Target((os.environ.get('BIND_HOST', '0.0.0.0'), int(os.environ.get('PORT', '8080'))), config).serve_forever()
