"""Strict allowlist contract: manifests never become arbitrary Docker options."""

import copy
import hashlib
import ipaddress
import json
import math
import re
import uuid
from pathlib import PurePosixPath

API = "cyberpod.infra/v1"
PREFIX = "io.cyberpod.infra"
ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
BLOCKED = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


class InfraError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message, code="INVALID_REQUEST"):
    if not condition:
        raise InfraError(code, message)


def obj(value, allowed, required=()):
    require(isinstance(value, dict), "Expected an object")
    require(set(value) <= set(allowed), "Unknown fields: " + ", ".join(sorted(set(value) - set(allowed))))
    require(set(required) <= set(value), "Required fields missing: " + ", ".join(sorted(set(required) - set(value))))


def number(value, low, high, integer=True):
    require(type(value) is int if integer else type(value) in (int, float), "Invalid numeric type")
    require(math.isfinite(value) and low <= value <= high, "Numeric value out of range")


def string(value, limit=4096):
    require(isinstance(value, str) and 0 < len(value) <= limit and "\0" not in value, "Invalid string")


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value) is not None, "Invalid identifier")
    return value


def session_id(value):
    try:
        require(isinstance(value, str) and str(uuid.UUID(value)) == value, "Use a canonical lowercase UUID")
    except (ValueError, AttributeError, TypeError):
        raise InfraError("INVALID_REQUEST", "Invalid session UUID") from None
    return value


def public_network(value):
    try:
        net = ipaddress.IPv4Network(value, strict=True)
    except (ValueError, TypeError):
        raise InfraError("INVALID_REQUEST", "Expected a canonical IPv4 CIDR") from None
    require(not any(net.overlaps(ipaddress.ip_network(x)) for x in BLOCKED), "Egress allowlist must contain public IPv4 ranges only")
    require(net.network_address.is_global and net.broadcast_address.is_global, "Egress range is not global unicast")
    return net


def writable_path(value):
    string(value, 120)
    path = PurePosixPath(value)
    require(path.is_absolute() and str(path) == value and ".." not in path.parts, "Invalid mount path")
    allowed = value in ("/tmp", "/run", "/var/tmp") or value.startswith(("/home/", "/work/", "/data/", "/app/", "/var/lib/", "/var/log/"))
    require(allowed, "Mount must be under an approved application data directory")
    return value


def validate_request(raw):
    spec = copy.deepcopy(raw)
    obj(spec, ("api_version", "session_id", "lab_id", "expires_at", "startup_timeout_seconds", "network", "volumes", "services"),
        ("api_version", "session_id", "lab_id", "expires_at", "network", "services"))
    require(spec["api_version"] == API, "Unsupported api_version")
    session_id(spec["session_id"])
    identifier(spec["lab_id"])
    number(spec["expires_at"], 1, 4102444800)
    spec.setdefault("startup_timeout_seconds", 120)
    number(spec["startup_timeout_seconds"], 10, 600)
    net = spec["network"]
    obj(net, ("internet", "allow"), ("internet",))
    require(net["internet"] in ("none", "restricted", "internet"), "Invalid Internet policy")
    net.setdefault("allow", [])
    require(isinstance(net["allow"], list) and len(net["allow"]) <= 64, "Invalid egress rules")
    require(net["internet"] == "restricted" or not net["allow"], "Allow rules only apply to restricted mode")
    for rule in net["allow"]:
        obj(rule, ("cidr", "protocol", "port"), ("cidr", "protocol", "port"))
        public_network(rule["cidr"])
        require(rule["protocol"] in ("tcp", "udp"), "Only TCP and UDP egress rules are supported")
        number(rule["port"], 1, 65535)
    spec.setdefault("volumes", [])
    require(isinstance(spec["volumes"], list) and len(spec["volumes"]) <= 8, "Invalid volumes")
    volumes = {}
    for vol in spec["volumes"]:
        obj(vol, ("id", "size_mb", "uid", "gid"), ("id", "size_mb", "uid", "gid"))
        identifier(vol["id"])
        require(vol["id"] not in volumes, "Duplicate volume")
        number(vol["size_mb"], 1, 2048)
        number(vol["uid"], 1, 65534)
        number(vol["gid"], 1, 65534)
        volumes[vol["id"]] = vol
    services = spec["services"]
    require(isinstance(services, list) and 1 <= len(services) <= 8, "Expected 1 to 8 services")
    names, used_volumes = set(), set()
    for service in services:
        obj(service, ("id", "image", "user", "command", "environment", "resources", "tmpfs", "volumes", "healthcheck", "endpoints"),
            ("id", "image", "user", "resources", "healthcheck"))
        identifier(service["id"])
        require(service["id"] not in names, "Duplicate service")
        names.add(service["id"])
        string(service["image"], 255)
        require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", service["image"]) is not None, "Invalid image reference")
        require(isinstance(service["user"], str) and re.fullmatch(r"[1-9][0-9]{0,4}:[1-9][0-9]{0,4}", service["user"]) is not None, "A numeric non-root uid:gid is required")
        require(all(int(x) <= 65534 for x in service["user"].split(":")), "UID/GID out of range")
        if "command" in service:
            require(isinstance(service["command"], list) and 1 <= len(service["command"]) <= 40, "Use an argv command array")
            for arg in service["command"]:
                string(arg)
        env = service.setdefault("environment", {})
        require(isinstance(env, dict) and len(env) <= 64, "Invalid environment")
        for key, value in env.items():
            require(re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", key) is not None and not key.startswith("CYBERPOD_"), "Reserved or invalid environment key")
            string(value)
        res = service["resources"]
        obj(res, ("cpus", "memory_mb", "pids", "shm_mb"), ("cpus", "memory_mb", "pids", "shm_mb"))
        number(res["cpus"], .1, 4, integer=False)
        number(res["memory_mb"], 64, 4096)
        number(res["pids"], 16, 512)
        number(res["shm_mb"], 8, 512)
        mounts = []
        tmpfs = service.setdefault("tmpfs", [])
        require(isinstance(tmpfs, list) and len(tmpfs) <= 12, "Invalid tmpfs mounts")
        for mount in tmpfs:
            obj(mount, ("path", "size_mb", "exec"), ("path", "size_mb"))
            mounts.append(writable_path(mount["path"]))
            number(mount["size_mb"], 1, 2048)
            mount.setdefault("exec", False)
            require(type(mount["exec"]) is bool, "Invalid exec flag")
        service.setdefault("volumes", [])
        require(isinstance(service["volumes"], list) and len(service["volumes"]) <= 8, "Invalid service volumes")
        for mount in service["volumes"]:
            obj(mount, ("volume", "target", "read_only"), ("volume", "target"))
            require(mount["volume"] in volumes, "Unknown volume")
            used_volumes.add(mount["volume"])
            mounts.append(writable_path(mount["target"]))
            mount.setdefault("read_only", False)
            require(type(mount["read_only"]) is bool, "Invalid read_only flag")
        require(len(mounts) == len(set(mounts)), "Duplicate mount target")
        require(not any(a != b and b.startswith(a + "/") for a in mounts for b in mounts), "Overlapping mount targets")
        storage = res["shm_mb"] + sum(m["size_mb"] for m in tmpfs) + sum(volumes[m["volume"]]["size_mb"] for m in service["volumes"])
        require(storage <= res["memory_mb"] * .75, "Writable storage budget must leave at least 25% RAM for processes")
        hc = service["healthcheck"]
        obj(hc, ("test", "interval_seconds", "timeout_seconds", "retries", "start_period_seconds"), ("test",))
        require(isinstance(hc["test"], list) and 2 <= len(hc["test"]) <= 30 and hc["test"][0] == "CMD", "Health checks require CMD argv form")
        for arg in hc["test"]:
            string(arg)
        for key, default, lower, upper in (("interval_seconds", 5, 1, 30), ("timeout_seconds", 2, 1, 10), ("retries", 5, 1, 30), ("start_period_seconds", 10, 0, 120)):
            hc.setdefault(key, default)
            number(hc[key], lower, upper)
        require(hc["timeout_seconds"] <= hc["interval_seconds"], "Health timeout must not exceed interval")
        eps = service.setdefault("endpoints", [])
        require(isinstance(eps, list) and len(eps) <= 8, "Invalid endpoints")
        ep_names = set()
        for ep in eps:
            obj(ep, ("name", "port", "protocol"), ("name", "port", "protocol"))
            identifier(ep["name"])
            require(ep["name"] not in ep_names, "Duplicate endpoint name")
            ep_names.add(ep["name"])
            number(ep["port"], 1024, 65535)
            require(ep["protocol"] in ("http", "https", "tcp"), "Invalid endpoint protocol")
    require(used_volumes == set(volumes), "Unused volumes are not allowed")
    return spec


def budget(spec):
    services = spec["services"]
    return {
        "cpu": round(sum(s["resources"]["cpus"] for s in services), 3),
        "memory_mb": sum(s["resources"]["memory_mb"] for s in services),
        "pids": sum(s["resources"]["pids"] for s in services),
        "storage_mb": sum(s["resources"]["shm_mb"] + sum(x["size_mb"] for x in s["tmpfs"]) for s in services) + sum(v["size_mb"] for v in spec["volumes"]),
    }


def fingerprint(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def names(instance, sid):
    token = hashlib.sha256(f"{instance}:{session_id(sid)}".encode()).hexdigest()[:12]
    return {"project": "cp-" + token, "network": "cp-" + token + "-lab", "bridge": "cp" + token, "chain": "CPS" + token}


def labels(instance, spec, generation):
    return {f"{PREFIX}.managed": "true", f"{PREFIX}.instance": instance,
            f"{PREFIX}.session": spec["session_id"], f"{PREFIX}.generation": generation,
            f"{PREFIX}.lab": spec["lab_id"], f"{PREFIX}.expires": str(spec["expires_at"])}


def compose(spec, instance, generation, image_ids=None):
    """A Compose document with external resources owned by this adapter."""
    n = names(instance, spec["session_id"])
    tags = labels(instance, spec, generation)
    result = {"name": n["project"], "services": {}, "networks": {"lab": {"external": True, "name": n["network"]}}}
    if spec["volumes"]:
        result["volumes"] = {v["id"]: {"external": True, "name": n["project"] + "-" + v["id"]} for v in spec["volumes"]}
    for s in spec["services"]:
        r = s["resources"]
        uid, gid = s["user"].split(":")
        env = dict(s["environment"], CYBERPOD_SESSION_ID=spec["session_id"], CYBERPOD_LAB_ID=spec["lab_id"], CYBERPOD_SERVICE_ID=s["id"], CYBERPOD_EXPIRES_AT=str(spec["expires_at"]))
        service = {
            "image": (image_ids or {}).get(s["image"], s["image"]), "pull_policy": "never",
            "user": s["user"], "init": True, "read_only": True, "privileged": False,
            "cgroup": "private", "ipc": "private",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "restart": "no", "stop_grace_period": "10s", "network_mode": None,
            "networks": {"lab": {"aliases": [s["id"]]}}, "dns": ["127.0.0.1"],
            "dns_search": ["."], "dns_opt": ["timeout:1", "attempts:1"],
            "sysctls": {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1", "net.ipv4.ip_forward": "0"},
            "cpus": r["cpus"], "mem_limit": f'{r["memory_mb"]}m', "memswap_limit": f'{r["memory_mb"]}m',
            "pids_limit": r["pids"], "shm_size": f'{r["shm_mb"]}m',
            "ulimits": {"nofile": {"soft": 1024, "hard": 4096}, "core": {"soft": 0, "hard": 0}},
            "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2", "compress": "false", "mode": "non-blocking", "max-buffer-size": "1m"}},
            "labels": dict(tags, **{f"{PREFIX}.service": s["id"]}), "environment": env,
            "healthcheck": {"test": s["healthcheck"]["test"], "interval": f'{s["healthcheck"]["interval_seconds"]}s', "timeout": f'{s["healthcheck"]["timeout_seconds"]}s', "retries": s["healthcheck"]["retries"], "start_period": f'{s["healthcheck"]["start_period_seconds"]}s'},
        }
        del service["network_mode"]
        if "command" in s:
            service["command"] = s["command"]
        if s["tmpfs"]:
            service["tmpfs"] = [f'{x["path"]}:rw,nosuid,nodev,{"exec" if x["exec"] else "noexec"},size={x["size_mb"]}m,uid={uid},gid={gid},mode=0700' for x in s["tmpfs"]]
        if s["volumes"]:
            service["volumes"] = [{"type": "volume", "source": x["volume"], "target": x["target"], "read_only": x["read_only"], "volume": {"nocopy": True}} for x in s["volumes"]]
        result["services"][s["id"]] = service
    return result


def escape_compose(value):
    """Prevent Compose interpolation of environment values, commands and health checks."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, dict):
        return {key: escape_compose(item) for key, item in value.items()}
    if isinstance(value, list):
        return [escape_compose(item) for item in value]
    return value
