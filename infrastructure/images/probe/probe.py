"""Harmless infrastructure fixture. No authentication scenario or flags."""

import http.server
import json
import os
import socket
import sys
import urllib.request


class Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/healthz"):
            self.send_error(404)
            return
        data = json.dumps({"ready": True, "session_id": os.environ.get("CYBERPOD_SESSION_ID"), "service_id": os.environ.get("CYBERPOD_SERVICE_ID")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def main():
    mode = sys.argv[1]
    if mode == "serve":
        http.server.HTTPServer(("0.0.0.0", 8080), Probe).serve_forever()
    elif mode == "health":
        with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=.7) as reply:
            assert reply.status == 200
    elif mode == "http":
        with urllib.request.urlopen(sys.argv[2], timeout=2) as reply:
            print(reply.read(8192).decode())
    elif mode == "tcp":
        with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=1.5):
            print("connected")
    elif mode == "dns":
        print(json.dumps(socket.gethostbyname_ex(sys.argv[2])))
    else:
        raise SystemExit("Unknown fixture operation")


if __name__ == "__main__":
    main()
