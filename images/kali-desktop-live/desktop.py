from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html, json, os, urllib.request
TARGET = os.environ.get('TARGET_URL', 'http://target:8080')
PORT = int(os.environ.get('CYBERPOD_NOVNC_PORT', '6080'))
class Handler(BaseHTTPRequestHandler):
    server_version = 'CyberPodKali/1'
    sys_version = ''
    def log_message(self, *args):
        return
    def reply(self, status, body, content='text/html; charset=utf-8'):
        data = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path in ('/health', '/ready'):
            return self.reply(200, 'ready', 'text/plain')
        if self.path in ('/', '/vnc.html', '/desktop.html'):
            return self.reply(200, '<h1>CyberPod connectivity probe</h1><p>This fixture is not a Kali desktop.</p><p>Target: '+html.escape(TARGET)+'</p>')
        if self.path == '/probe':
            try:
                with urllib.request.urlopen(TARGET + '/health', timeout=3) as response:
                    payload = {'target': TARGET, 'status': response.status, 'body': response.read().decode()[:80]}
            except Exception as exc:
                payload = {'target': TARGET, 'error': type(exc).__name__}
            return self.reply(200, json.dumps(payload), 'application/json')
        return self.reply(404, 'not found', 'text/plain')
if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
