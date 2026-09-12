"""Application readiness endpoint contract for optional Python target images."""

import os
import urllib.request

url = os.environ.get("HEALTH_URL", "http://127.0.0.1:8080/healthz")
with urllib.request.urlopen(url, timeout=1.5) as response:
    raise SystemExit(0 if response.status == 200 else 1)
