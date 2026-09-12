"""Single-worker lifecycle adapter, durable intent, admission and label cleanup."""

import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

from .firewall import Firewall, session_rules
from .host import Runner, connected_cidrs, doctor
from .model import (API, PREFIX, InfraError, budget, compose, escape_compose, fingerprint,
                    labels, names, require, session_id, validate_request)

LOG = logging.getLogger("cyberpod.infra")
ACTIVE = {"STARTING", "READY", "FAILED", "STOPPING", "CLEANUP_FAILED"}


def atomic_json(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Engine:
    def __init__(self, policy, runner=None, firewall=None, clock=time.time, preflight=doctor, routes=connected_cidrs):
        self.policy = policy
        self.runner = runner or Runner()
        self.firewall = firewall or Firewall(self.runner)
        self.clock, self.preflight, self.routes = clock, preflight, routes
        self.root = Path(policy.state_dir)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        stat = self.root.lstat()
        require(not self.root.is_symlink() and stat.st_uid == os.getuid() and stat.st_mode & 0o077 == 0,
                "State directory must be private and owned by the worker user", "INVALID_POLICY")

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def path(self, sid):
        return self.root / (session_id(sid) + ".json")

    def read(self, sid):
        try:
            state = json.loads(self.path(sid).read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise InfraError("STATE_CORRUPT", "Cannot read session state; preserve it and recover by labels") from None
        require(state["instance"] == self.policy.instance, "State belongs to another worker instance", "STATE_CONFLICT")
        return state

    def states(self):
        return [self.read(path.stem) for path in sorted(self.root.glob("*.json")) if path.stem != "policy"]

    def save(self, state):
        state["updated_at"] = int(self.clock())
        atomic_json(self.path(state["session_id"]), state)

    def _selector(self, sid=None):
        values = {f"{PREFIX}.managed": "true", f"{PREFIX}.instance": self.policy.instance}
        if sid:
            values[f"{PREFIX}.session"] = session_id(sid)
        result = []
        for key, value in values.items():
            result += ["--filter", f"label={key}={value}"]
        return result

    def discover(self, sid=None):
        result = {}
        for kind in ("container", "network", "volume"):
            args = [kind, "ls", "-q"]
            if kind == "container":
                args.append("-a")
            result[kind] = self.runner.docker(*args, *self._selector(sid)).stdout.split()
        return result

    def inspect(self, kind, ids):
        return self.runner.docker_json(kind, "inspect", *ids) if ids else []

    def _owned(self, kind, item, sid):
        tags = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
        return tags and tags.get(f"{PREFIX}.managed") == "true" and tags.get(f"{PREFIX}.instance") == self.policy.instance and tags.get(f"{PREFIX}.session") == sid

    def allocate(self):
        used = []
        for state in self.states():
            if state["status"] in ACTIVE and state.get("subnet"):
                used.append(ipaddress.ip_network(state["subnet"]))
        all_ids = self.runner.docker("network", "ls", "-q").stdout.split()
        for network in self.inspect("network", all_ids):
            for config in network.get("IPAM", {}).get("Config", []) or []:
                if config.get("Subnet"):
                    net = ipaddress.ip_network(config["Subnet"])
                    if net.version == 4:
                        used.append(net)
        used.extend(ipaddress.ip_network(n) for n in self.routes(self.runner))
        pool = ipaddress.ip_network(self.policy.subnet_pool)
        for subnet in pool.subnets(new_prefix=self.policy.subnet_prefix):
            if not any(subnet.overlaps(n) for n in used):
                return str(subnet)
        raise InfraError("CAPACITY_EXCEEDED", "No non-overlapping session subnet available; check worker pool and routes")

    def admit(self, spec):
        active = [s for s in self.states() if s["status"] in ACTIVE]
        # Lost state must not silently release admission reservations.
        known = {s["session_id"] for s in active}
        resources = self.discover()
        for kind, ids in resources.items():
            for item in self.inspect(kind, ids):
                tags = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
                require(tags.get(f"{PREFIX}.session") in known, "Orphan resources exist; run reap before admitting sessions", "ORPHAN_RESOURCES")
        require(len(active) < self.policy.max_sessions, "Worker session capacity reached", "CAPACITY_EXCEEDED")
        requested = budget(spec)
        for key, value in requested.items():
            require(value <= getattr(self.policy, "session_" + key), "Session exceeds " + key + " budget", "CAPACITY_EXCEEDED")
            current = sum(s["budget"][key] for s in active)
            require(current + value <= getattr(self.policy, "capacity_" + key), "Worker exceeds " + key + " budget", "CAPACITY_EXCEEDED")
        return requested

    def images(self, spec):
        resolved = {}
        for s in spec["services"]:
            image = s["image"]
            if image in resolved:
                continue
            require(image in self.policy.allowed_images, "Image is not in the operator allowlist", "IMAGE_REJECTED")
            require(not self.policy.require_digest or "@sha256:" in image, "Production images must use immutable digests", "IMAGE_REJECTED")
            info = self.runner.docker_json("image", "inspect", image)[0]
            require(not info.get("Config", {}).get("Volumes"), "Image VOLUME declarations are prohibited; declare bounded mounts in the infra request", "IMAGE_REJECTED")
            require(info.get("Size", 0) <= self.policy.max_image_mb * 1048576, "Image exceeds size policy", "IMAGE_REJECTED")
            resolved[image] = info["Id"]
        return resolved

    def _compose(self, state, *args):
        return self.runner.docker("compose", "--project-name", state["names"]["project"],
                                  "--project-directory", str(self.root), "--env-file", "/dev/null",
                                  "--file", str(self.root / (state["session_id"] + ".compose")), *args, timeout=120)

    def start(self, raw):
        spec = validate_request(raw)
        with self.lock():
            return self._start(spec)

    def _start(self, spec, restarting=False):
        sid = spec["session_id"]
        existing = self.read(sid)
        if existing and not restarting:
            require(existing["spec_hash"] == fingerprint(spec), "Session UUID already has a different request", "STATE_CONFLICT")
            require(existing["status"] != "CLEANED", "Use restart or a new session UUID after cleanup", "STATE_CONFLICT")
            return self._status(existing)
        now = int(self.clock())
        require(now < spec["expires_at"] <= now + self.policy.max_ttl_seconds, "Expiration must be in the future and within the worker TTL", "INVALID_REQUEST")
        self.preflight(self.policy, self.runner)
        self.firewall.install()
        self.firewall.audit()
        resources = self.admit(spec)
        image_ids = self.images(spec)
        subnet = self.allocate()
        n = names(self.policy.instance, sid)
        generation = str(uuid.uuid4())
        protected = sorted(set(self.policy.protected_cidrs) | set(self.routes(self.runner)))
        state = {
            "api_version": API, "instance": self.policy.instance, "session_id": sid,
            "lab_id": spec["lab_id"], "generation": generation, "spec_hash": fingerprint(spec),
            "created_at": now, "expires_at": spec["expires_at"], "startup_deadline": now + spec["startup_timeout_seconds"],
            "status": "STARTING", "budget": resources, "subnet": subnet, "names": n,
            "services": {s["id"]: {"endpoints": s["endpoints"]} for s in spec["services"]},
            "firewall_rules": session_rules(n["bridge"], subnet, spec["network"]["internet"], spec["network"]["allow"], protected),
            "internet": spec["network"]["internet"], "error": None, "resources": {"container": [], "network": [], "volume": []},
        }
        # Reserve capacity and record cleanup identity before any Docker mutation.
        self.save(state)
        try:
            self.firewall.add(state)
            tags = labels(self.policy.instance, spec, generation)
            label_args = [arg for key, value in tags.items() for arg in ("--label", key + "=" + value)]
            network_args = ["network", "create", "--driver", "bridge", "--subnet", subnet,
                            "--opt", "com.docker.network.bridge.name=" + n["bridge"],
                            "--opt", "com.docker.network.bridge.enable_icc=true",
                            "--opt", "com.docker.network.bridge.host_binding_ipv4=127.0.0.1"]
            if spec["network"]["internet"] == "none":
                network_args.append("--internal")
            network_id = self.runner.docker(*network_args, *label_args, n["network"]).stdout.strip()
            actual_network = self.inspect("network", [network_id])[0]
            require(self._owned("network", actual_network, sid) and not actual_network.get("EnableIPv6") and
                    actual_network.get("Internal") == (spec["network"]["internet"] == "none") and
                    actual_network.get("Options", {}).get("com.docker.network.bridge.name") == n["bridge"],
                    "Network isolation settings differ from policy", "SANDBOX_MISMATCH")
            for vol in spec["volumes"]:
                options = f'size={vol["size_mb"]}m,uid={vol["uid"]},gid={vol["gid"]},mode=0700,nosuid,nodev,noexec'
                volume_name = n["project"] + "-" + vol["id"]
                self.runner.docker("volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt", "o=" + options, *label_args, volume_name)
                actual_volume = self.inspect("volume", [volume_name])[0]
                require(self._owned("volume", actual_volume, sid) and actual_volume.get("Labels", {}).get(f"{PREFIX}.generation") == generation and
                        actual_volume.get("Driver") == "local" and actual_volume.get("Options") == {"type": "tmpfs", "device": "tmpfs", "o": options},
                        "Volume ownership or storage limit differs from policy", "SANDBOX_MISMATCH")
            document = compose(spec, self.policy.instance, generation, image_ids)
            if spec["network"]["internet"] == "internet":
                for service in document["services"].values():
                    service["dns"] = self.policy.internet_dns
            atomic_json(self.root / (sid + ".compose"), escape_compose(document))
            self._compose(state, "create", "--no-build", "--pull", "never")
            self.verify_created(state, spec)
            # Rules are audited before the first workload instruction executes.
            self.firewall.audit(state)
            self._compose(state, "start")
            state["resources"] = self.discover(sid)
            self.save(state)
            LOG.info("session_created session=%s generation=%s", sid, generation)
            return self._status(state)
        except Exception as exc:
            code = exc.code if isinstance(exc, InfraError) else "INTERNAL_ERROR"
            state["error"] = code
            if code == "COMMAND_TIMEOUT":
                # A killed CLI may leave a daemon operation in flight. Retain its
                # reservation and guard across repeated reconciliations.
                state["cleanup_not_before"] = int(self.clock()) + 300
            state["status"] = "FAILED"
            self.save(state)
            result = self._cleanup(state)
            result["error"] = code
            self.save(result)
            # CLEANED with error reports a rolled-back failed start, never READY.
            LOG.error("session_start_failed session=%s code=%s cleanup=%s", sid, code, result["status"])
            return self.public(result)

    def verify_created(self, state, spec):
        items = self.inspect("container", self.discover(state["session_id"])["container"])
        require(len(items) == len(spec["services"]), "Unexpected container count", "SANDBOX_MISMATCH")
        seen = set()
        for item in items:
            config, host = item["Config"], item["HostConfig"]
            sid = config["Labels"].get(f"{PREFIX}.service")
            require(sid not in seen, "Duplicate service container", "SANDBOX_MISMATCH")
            seen.add(sid)
            service = next((s for s in spec["services"] if s["id"] == sid), None)
            require(service is not None and self._owned("container", item, state["session_id"]), "Unexpected container ownership", "SANDBOX_MISMATCH")
            r = service["resources"]
            require(host["ReadonlyRootfs"] and not host["Privileged"] and host.get("CapDrop") == ["ALL"] and not host.get("CapAdd"), "Container privilege policy mismatch", "SANDBOX_MISMATCH")
            require("no-new-privileges:true" in host["SecurityOpt"] and config["User"] == service["user"], "Container identity policy mismatch", "SANDBOX_MISMATCH")
            require(host.get("CgroupnsMode") == "private" and host.get("IpcMode") == "private" and host.get("PidMode", "") == "", "Unexpected shared host namespace", "SANDBOX_MISMATCH")
            require(host["Memory"] == r["memory_mb"] * 1048576 and host["MemorySwap"] == host["Memory"] and host["PidsLimit"] == r["pids"] and host["NanoCpus"] == int(r["cpus"] * 1e9), "Container resource limits were not enforced", "SANDBOX_MISMATCH")
            require(host.get("ShmSize") == r["shm_mb"] * 1048576, "Shared-memory limit was not enforced", "SANDBOX_MISMATCH")
            require(not host.get("PortBindings") and not host.get("Devices") and not host.get("Binds"), "Unexpected host exposure", "SANDBOX_MISMATCH")
            require(set(item["NetworkSettings"]["Networks"]) == {state["names"]["network"]}, "Unexpected network attachment", "SANDBOX_MISMATCH")
            require(config.get("Healthcheck", {}).get("Test") == service["healthcheck"]["test"], "Health check mismatch", "SANDBOX_MISMATCH")
            allowed_volumes = {state["names"]["project"] + "-" + m["volume"] for m in service["volumes"]}
            require(all(m["Type"] in ("tmpfs", "volume") and (m["Type"] != "volume" or m["Name"] in allowed_volumes) for m in item.get("Mounts", [])), "Unexpected storage mount", "SANDBOX_MISMATCH")

    def status(self, sid):
        with self.lock():
            state = self.read(sid)
            require(state is not None, "Session not found", "NOT_FOUND")
            return self._status(state)

    def _status(self, state):
        if state["status"] not in ("STARTING", "READY"):
            return self.public(state)
        if self.clock() >= state["expires_at"]:
            state["error"] = "SESSION_EXPIRED"
            return self.public(self._cleanup(state))
        try:
            self.firewall.audit(state)
        except InfraError as exc:
            state["error"] = exc.code
            # Do not keep active workloads under a known missing/modified guard.
            return self.public(self._cleanup(state))
        found = self.discover(state["session_id"])
        containers = self.inspect("container", found["container"])
        details, failed = {}, None
        if len(found["network"]) != 1:
            failed = "NETWORK_MISSING"
        for item in containers:
            tags = item["Config"]["Labels"]
            service = tags.get(f"{PREFIX}.service")
            if service not in state["services"] or service in details or tags.get(f"{PREFIX}.generation") != state["generation"]:
                failed = "RESOURCE_MISMATCH"
                continue
            runtime = item["State"]
            health = runtime.get("Health", {}).get("Status", "missing")
            net = item["NetworkSettings"]["Networks"]
            if set(net) != {state["names"]["network"]}:
                failed = "NETWORK_MISMATCH"
            if not runtime.get("Running") or runtime.get("Paused") or runtime.get("Restarting") or health in ("unhealthy", "missing"):
                failed = "CONTAINER_UNHEALTHY"
            endpoint = net.get(state["names"]["network"], {})
            details[service] = {"container_id": item["Id"], "health": health, "running": runtime.get("Running", False),
                                "ip": endpoint.get("IPAddress"), "oom_killed": runtime.get("OOMKilled", False)}
        if set(details) != set(state["services"]):
            failed = "CONTAINER_MISSING"
        ready = bool(details) and all(d["health"] == "healthy" and d["ip"] for d in details.values())
        if not failed and not ready and self.clock() >= state["startup_deadline"]:
            failed = "STARTUP_TIMEOUT"
        state["status"] = "FAILED" if failed else ("READY" if ready else "STARTING")
        state["error"], state["resources"], state["observed"] = failed, found, details
        self.save(state)
        return self.public(state)

    @staticmethod
    def public(state):
        result = {k: state[k] for k in ("api_version", "instance", "session_id", "lab_id", "generation", "status", "created_at", "expires_at", "error", "resources", "budget")}
        result["network"] = {"name": state["names"]["network"], "subnet": state.get("subnet"), "internet": state.get("internet")}
        result["services"] = state.get("observed", {})
        result["cleanup_errors"] = state.get("cleanup_errors", [])
        result["endpoints"] = []
        if state["status"] == "READY":
            for sid, service in state["services"].items():
                for ep in service["endpoints"]:
                    result["endpoints"].append(dict(ep, service_id=sid, address=state["observed"][sid]["ip"], scope="worker-host-only"))
        return result

    def stop(self, sid, expected_generation=None):
        with self.lock():
            state = self.read(sid) or self.orphan_state(sid)
            self.check_generation(state, expected_generation)
            return self.public(self._cleanup(state))

    @staticmethod
    def check_generation(state, expected):
        require(expected is None or expected == state["generation"], "Stale session generation", "STATE_CONFLICT")

    def orphan_state(self, sid):
        return {"api_version": API, "instance": self.policy.instance, "session_id": session_id(sid), "lab_id": "unknown",
                "generation": "unknown", "spec_hash": "unknown", "created_at": int(self.clock()), "expires_at": 0,
                "status": "STOPPING", "budget": {"cpu": 0, "memory_mb": 0, "pids": 0, "storage_mb": 0},
                "names": names(self.policy.instance, sid), "services": {}, "error": None, "resources": {}}

    def _cleanup(self, state):
        sid = state["session_id"]
        state["status"] = "STOPPING"
        self.save(state)
        errors = []
        try:
            found = self.discover(sid)
            # Exact ownership labels, containers first, no host-wide prune.
            for kind in ("container", "volume", "network"):
                for rid in found[kind]:
                    try:
                        inspected = self.inspect(kind, [rid])
                        require(inspected and self._owned(kind, inspected[0], sid), "Refusing to remove unowned resource", "OWNERSHIP_MISMATCH")
                        if kind == "container":
                            self.runner.docker("container", "stop", "--time", "10", rid, timeout=20, check=False)
                            self.runner.docker("container", "rm", "--force", "--volumes", rid)
                        else:
                            self.runner.docker(kind, "rm", rid)
                    except InfraError as exc:
                        errors.append(exc.code)
            remaining = self.discover(sid)
            state["resources"] = remaining
            if not any(remaining.values()) and self.clock() >= state.get("cleanup_not_before", 0):
                self.firewall.remove(state["names"])
                (self.root / (sid + ".compose")).unlink(missing_ok=True)
                state["status"] = "CLEANED"
                state["observed"] = {}
            else:
                state["status"] = "CLEANUP_FAILED"
        except InfraError as exc:
            errors.append(exc.code)
            state["status"] = "CLEANUP_FAILED"
        state["cleanup_errors"] = sorted(set(errors)) if state["status"] != "CLEANED" else []
        self.save(state)
        LOG.info("session_cleanup session=%s status=%s", sid, state["status"])
        return state

    def restart(self, raw, expected_generation):
        spec = validate_request(raw)
        with self.lock():
            existing = self.read(spec["session_id"])
            require(existing is not None, "Session not found", "NOT_FOUND")
            require(spec["lab_id"] == existing["lab_id"], "Restart must preserve the Session Lab association", "STATE_CONFLICT")
            require(expected_generation is not None, "Restart requires expected_generation", "INVALID_REQUEST")
            self.check_generation(existing, expected_generation)
            cleaned = self._cleanup(existing)
            require(cleaned["status"] == "CLEANED", "Restart blocked until cleanup succeeds", "CLEANUP_FAILED")
            return self._start(spec, restarting=True)

    def reap(self):
        with self.lock():
            states = {s["session_id"]: s for s in self.states()}
            orphans = set()
            for kind, ids in self.discover().items():
                for item in self.inspect(kind, ids):
                    tags = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
                    sid = tags.get(f"{PREFIX}.session")
                    session_id(sid)
                    if sid not in states or states[sid]["status"] == "CLEANED":
                        orphans.add(sid)
            results = []
            for sid in sorted(orphans):
                results.append(self.public(self._cleanup(self.orphan_state(sid))))
            for sid, state in states.items():
                if sid in orphans:
                    continue
                if state["status"] in ("STARTING", "READY"):
                    self._status(state)
                if state["status"] in ("FAILED", "STOPPING", "CLEANUP_FAILED") or (state["status"] != "CLEANED" and self.clock() >= state["expires_at"]):
                    results.append(self.public(self._cleanup(state)))
                elif state["status"] == "CLEANED" and self.clock() - state["updated_at"] >= self.policy.retained_state_seconds:
                    self.path(sid).unlink()
            # Remove incomplete atomic-write files only while holding the worker lock.
            for path in self.root.glob(".write-*"):
                path.unlink()
            return {"api_version": API, "sessions": results}

    def wait(self, sid, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.status(sid)
            if result["status"] != "STARTING":
                return result
            time.sleep(1)
        # A caller timeout is not authority to stop a potentially newer generation.
        raise InfraError("WAIT_TIMEOUT", "Wait expired; poll status or stop using the current generation")
