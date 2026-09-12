"""Emit fresh sample requests. Images are inputs, not hard-coded Lab dispatch."""

import argparse
import copy
import json
import time
import uuid


def request(sid=None, image="cyberpod/infra-probe:dev", internet="none"):
    service = {
        "id": "client", "image": image, "user": "1000:1000",
        "resources": {"cpus": .5, "memory_mb": 256, "pids": 64, "shm_mb": 16},
        "tmpfs": [{"path": "/tmp", "size_mb": 16}],
        "volumes": [{"volume": "scratch", "target": "/work/shared"}],
        "healthcheck": {"test": ["CMD", "python3", "/probe.py", "health"], "interval_seconds": 2, "timeout_seconds": 1, "start_period_seconds": 2, "retries": 10},
        "endpoints": [{"name": "http", "port": 8080, "protocol": "http"}],
    }
    target = copy.deepcopy(service)
    target.update(id="target", volumes=[])
    return {
        "api_version": "cyberpod.infra/v1", "session_id": sid or str(uuid.uuid4()),
        "lab_id": "infra-hello", "expires_at": int(time.time()) + 1800,
        "startup_timeout_seconds": 45,
        "network": {"internet": internet, "allow": []},
        "volumes": [{"id": "scratch", "size_mb": 32, "uid": 1000, "gid": 1000}],
        "services": [service, target],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    parser.add_argument("--image", default="cyberpod/infra-probe:dev")
    parser.add_argument("--internet", choices=("none", "restricted", "internet"), default="none")
    args = parser.parse_args()
    print(json.dumps(request(args.session, args.image, args.internet), indent=2))
