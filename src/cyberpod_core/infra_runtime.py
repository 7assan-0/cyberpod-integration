"""Runtime adapter from Core SessionContext to Astra #2 cyberpod.infra/v1."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

from .errors import CoreError, ProviderCancelled
from .models import EndpointRef, ResourceRef, RuntimeSnapshot, SessionContext

API = "cyberpod.infra/v1"
INTERNET = {"none": "none", "restricted": "restricted", "allowed": "internet"}
INFRA_TO_HEALTH = {
    "STARTING": "STARTING",
    "READY": "READY",
    "FAILED": "FAILED",
    "STOPPING": "STOPPED",
    "CLEANUP_FAILED": "FAILED",
    "CLEANED": "STOPPED",
}


def _safe_mount(path: str, volume_id: str) -> str:
    allowed = path in {"/tmp", "/run", "/var/tmp"} or path.startswith(
        ("/home/", "/work/", "/data/", "/app/", "/var/lib/", "/var/log/")
    )
    return path if allowed else f"/work/{volume_id}"


def to_infra_request(context: SessionContext) -> dict:
    definition = context.definition
    uid = gid = 1000
    if definition.containers:
        uid_s, gid_s = definition.containers[0].run_as.split(":")
        uid, gid = int(uid_s), int(gid_s)
    volumes = [{
        "id": volume.id,
        "size_mb": max(1, min(int(volume.size_mb), 2048)),
        "uid": uid,
        "gid": gid,
    } for volume in definition.volumes]
    services = []
    for container in definition.containers:
        command = list(container.healthcheck.command)
        test = command if command[:1] == ["CMD"] else ["CMD", *command]
        service = {
            "id": container.id,
            "image": container.image,
            "user": container.run_as,
            "resources": {
                "cpus": max(0.1, min(float(container.resources.cpu), 4.0)),
                "memory_mb": max(64, min(int(container.resources.memory_mb), 4096)),
                "pids": max(16, min(int(container.resources.pids), 512)),
                "shm_mb": 16,
            },
            "healthcheck": {
                "test": test,
                "interval_seconds": container.healthcheck.interval_seconds,
                "timeout_seconds": container.healthcheck.timeout_seconds,
                "retries": container.healthcheck.retries,
            },
        }
        if container.command:
            service["command"] = list(container.command)
        if container.mounts:
            service["volumes"] = [{
                "volume": mount.volume,
                "target": _safe_mount(mount.path, mount.volume),
                "read_only": mount.read_only,
            } for mount in container.mounts]
        endpoints = [{"name": f"p{port}", "port": port, "protocol": "tcp"}
                     for port in container.ports if 1024 <= port <= 65535]
        if endpoints:
            service["endpoints"] = endpoints
        env = {key: value for key, value in container.environment.items() if not key.startswith("CYBERPOD_")}
        if env:
            service["environment"] = env
        services.append(service)
    network = definition.networks[0] if definition.networks else None
    internet = INTERNET.get(getattr(network, "internet", "none"), "none")
    allow = []
    if internet == "restricted" and network is not None:
        for raw in network.egress_allowlist:
            allow.append({"cidr": raw if "/" in raw else raw + "/32", "protocol": "tcp", "port": 443})
    return {
        "api_version": API,
        "session_id": str(context.session_id),
        "lab_id": context.lab_id,
        "expires_at": int(context.expires_at.timestamp()),
        "startup_timeout_seconds": 45,
        "network": {"internet": internet, "allow": allow},
        "volumes": volumes,
        "services": services,
    }


def snapshot_from_infra(context: SessionContext, payload: dict) -> RuntimeSnapshot:
    labels = context.labels
    generation = str(payload.get("generation") or "")
    grouped = payload.get("resources") or {}
    status = payload.get("status")
    resources = []

    def add(kind, logical_id, provider_id):
        resources.append(ResourceRef(
            provider_id=str(provider_id), kind=kind, logical_id=logical_id, labels=labels,
            metadata={"infra_generation": generation, "infra_status": str(status or "")},
        ))

    if status in {"STARTING", "READY", "FAILED", "CLEANUP_FAILED"}:
        for network in context.definition.networks:
            ids = grouped.get("network") or [f"net-{network.id}"]
            add("network", network.id, ids[0] if ids else f"net-{network.id}")
        for volume in context.definition.volumes:
            ids = grouped.get("volume") or [f"vol-{volume.id}"]
            match = next((item for item in ids if volume.id in str(item)), ids[0] if ids else f"vol-{volume.id}")
            add("volume", volume.id, match)
        for container in context.definition.containers:
            ids = grouped.get("container") or [f"ctr-{container.id}"]
            match = next((item for item in ids if container.id in str(item)), ids[0] if ids else f"ctr-{container.id}")
            add("container", container.id, match)
    health = INFRA_TO_HEALTH.get(payload.get("status"), "UNKNOWN")
    endpoints = []
    if health == "READY":
        for record in payload.get("endpoints") or []:
            kind = "browser" if record.get("protocol") in {"http", "https"} else "terminal"
            endpoints.append(EndpointRef(kind=kind, service_ref=f"{record.get('address') or '127.0.0.1'}:{record.get('port') or 0}"))
    return RuntimeSnapshot(health=health, resources=resources, endpoints=endpoints)


class MemoryInfraClient:
    simulated = True

    def __init__(self):
        self.sessions = {}

    def start(self, request):
        sid = request["session_id"]
        existing = self.sessions.get(sid)
        if existing and existing["status"] != "CLEANED":
            return existing
        generation = str(uuid4())
        state = {
            "api_version": API, "session_id": sid, "lab_id": request["lab_id"],
            "generation": generation, "status": "READY", "error": None,
            "resources": {
                "network": [f"net-{sid[:8]}"],
                "volume": [f"vol-{volume['id']}" for volume in request.get("volumes", [])],
                "container": [f"ctr-{service['id']}" for service in request["services"]],
            },
            "endpoints": [
                {"name": ep["name"], "service_id": service["id"], "protocol": ep["protocol"],
                 "address": "127.0.0.1", "port": ep["port"], "scope": "worker-host-only"}
                for service in request["services"] for ep in service.get("endpoints", [])
            ],
        }
        self.sessions[sid] = state
        return state

    def status(self, session_id):
        state = self.sessions.get(session_id)
        if state is None:
            raise CoreError("NOT_FOUND", "Infra session not found", 404)
        return state

    def stop(self, session_id, generation=None):
        state = self.sessions.get(session_id)
        if state is None:
            return {"api_version": API, "session_id": session_id, "status": "CLEANED",
                    "generation": generation or "", "resources": {}, "endpoints": [], "error": None}
        if generation and state.get("generation") != generation:
            raise CoreError("INVALID_STATE", "Infra generation mismatch")
        state["status"] = "CLEANED"
        state["resources"] = {"network": [], "volume": [], "container": []}
        state["endpoints"] = []
        return state

    def restart(self, request, generation):
        self.stop(request["session_id"], generation)
        self.sessions.pop(request["session_id"], None)
        return self.start(request)


class CliInfraClient:
    simulated = False

    def __init__(self, program=None, policy=None):
        self.program = program or [sys.executable, "-m", "cyberpod_infra"]
        self.policy = policy or os.environ.get("CYBERPOD_INFRA_POLICY", "/etc/cyberpod-infra/worker.json")

    def _run(self, args, payload=None):
        command = [*self.program, "--policy", self.policy, *args]
        encoded = None if payload is None else json.dumps(payload).encode()
        try:
            completed = subprocess.run(command, input=encoded, capture_output=True, timeout=120, check=False)
        except FileNotFoundError as exc:
            raise CoreError("RUNTIME_UNSUPPORTED", "cyberpod_infra CLI is not installed", 503) from exc
        except subprocess.TimeoutExpired as exc:
            raise CoreError("RUNTIME_TIMEOUT", "Infra worker timed out", 503) from exc
        try:
            body = json.loads(completed.stdout.decode() or "{}")
        except ValueError as exc:
            raise CoreError("RUNTIME_OPERATION_FAILED", "Infra worker returned invalid JSON", 503) from exc
        if completed.returncode not in {0, 1} or body.get("error"):
            code = (body.get("error") or {}).get("code") or "RUNTIME_OPERATION_FAILED"
            raise CoreError(code if code != "NOT_FOUND" else "NOT_FOUND",
                            "Infra worker rejected the operation", 503 if code != "NOT_FOUND" else 404)
        return body

    def start(self, request):
        return self._run(["start", "--wait"], request)

    def status(self, session_id):
        return self._run(["status", session_id])

    def stop(self, session_id, generation=None):
        args = ["stop", session_id]
        if generation:
            args.extend(["--generation", generation])
        return self._run(args)

    def restart(self, request, generation):
        return self._run(["restart", "--wait", "--generation", generation], request)


class InfraRuntime:
    name = "cyberpod-infra-v1"

    def __init__(self, client=None):
        self.client = client or MemoryInfraClient()
        self.simulated = bool(getattr(self.client, "simulated", False))
        self._generations = {}

    def validate_lab(self, definition):
        if not definition.containers or not definition.networks:
            raise CoreError("RUNTIME_UNSUPPORTED", "Infra runtime needs containers and a session network", 503)
        for container in definition.containers:
            uid, gid = container.run_as.split(":")
            if int(uid) == 0 or int(gid) == 0:
                raise CoreError("RUNTIME_UNSUPPORTED", "Infra runtime rejects root containers", 503)

    async def available(self):
        if self.simulated:
            return True
        try:
            self.client._run(["doctor"])
            return True
        except Exception:
            return False

    def _generation(self, context):
        return self._generations.get(str(context.session_id))

    async def start(self, context, cancel, report):
        self.validate_lab(context.definition)
        if cancel.is_set():
            raise ProviderCancelled()
        payload = await asyncio.to_thread(self.client.start, to_infra_request(context))
        if payload.get("status") in {"FAILED", "CLEANUP_FAILED", "CLEANED"} and payload.get("error"):
            raise CoreError("RUNTIME_OPERATION_FAILED", "Infra start failed", 503)
        self._generations[str(context.session_id)] = str(payload.get("generation") or "")
        snapshot = snapshot_from_infra(context, payload)
        for resource in snapshot.resources:
            await report(resource)
        return snapshot

    async def stop(self, context):
        await asyncio.to_thread(self.client.stop, str(context.session_id), self._generation(context))

    async def cleanup(self, context):
        await asyncio.to_thread(self.client.stop, str(context.session_id), self._generation(context))
        self._generations.pop(str(context.session_id), None)

    async def inspect(self, context):
        try:
            payload = await asyncio.to_thread(self.client.status, str(context.session_id))
        except CoreError as exc:
            if exc.code == "NOT_FOUND":
                return RuntimeSnapshot(health="STOPPED", resources=[], endpoints=[])
            raise
        return snapshot_from_infra(context, payload)


def memory_factory():
    return InfraRuntime(MemoryInfraClient())


def factory():
    mode = os.environ.get("CYBERPOD_INFRA_MODE", "memory")
    if mode == "cli":
        return InfraRuntime(CliInfraClient())
    return InfraRuntime(MemoryInfraClient())
