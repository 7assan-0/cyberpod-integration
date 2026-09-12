"""Trusted Linux worker policy and bounded local command execution."""

import ipaddress
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .model import InfraError, identifier, number, obj, public_network, require


@dataclass
class Policy:
    instance: str = "worker-01"
    state_dir: str = "/var/lib/cyberpod-infra"
    subnet_pool: str = "10.240.0.0/16"
    subnet_prefix: int = 28
    max_sessions: int = 4
    capacity_cpu: float = 6
    capacity_memory_mb: int = 12288
    capacity_pids: int = 2048
    capacity_storage_mb: int = 6144
    session_cpu: float = 3
    session_memory_mb: int = 3072
    session_pids: int = 512
    session_storage_mb: int = 1536
    host_reserved_memory_mb: int = 2048
    min_free_disk_mb: int = 4096
    max_image_mb: int = 4096
    max_ttl_seconds: int = 7200
    retained_state_seconds: int = 86400
    allowed_images: list = field(default_factory=list)
    internet_dns: list = field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    protected_cidrs: list = field(default_factory=list)
    require_digest: bool = True

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise InfraError("INVALID_POLICY", "Cannot read worker policy") from exc
        obj(data, cls.__dataclass_fields__)
        policy = cls(**data)
        policy.validate()
        return policy

    def validate(self):
        identifier(self.instance)
        require(Path(self.state_dir).is_absolute(), "state_dir must be absolute", "INVALID_POLICY")
        try:
            pool = ipaddress.IPv4Network(self.subnet_pool)
            private = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
            require(any(pool.subnet_of(n) for n in private), "Pool must be RFC1918", "INVALID_POLICY")
            require(pool.prefixlen <= self.subnet_prefix <= 28, "Invalid session subnet size", "INVALID_POLICY")
            for cidr in self.protected_cidrs:
                ipaddress.IPv4Network(cidr)
        except (ValueError, TypeError) as exc:
            raise InfraError("INVALID_POLICY", "Invalid network policy") from exc
        number(self.max_sessions, 1, 256)
        for key in ("capacity_cpu", "session_cpu"):
            number(getattr(self, key), .1, 4096, integer=False)
        for key in ("capacity_memory_mb", "capacity_pids", "capacity_storage_mb", "session_memory_mb", "session_pids", "session_storage_mb", "host_reserved_memory_mb", "min_free_disk_mb", "max_image_mb", "max_ttl_seconds", "retained_state_seconds"):
            number(getattr(self, key), 1, 2**31)
        require(type(self.require_digest) is bool, "Invalid require_digest", "INVALID_POLICY")
        require(isinstance(self.allowed_images, list) and all(isinstance(x, str) and x for x in self.allowed_images), "Invalid image allowlist", "INVALID_POLICY")
        require(isinstance(self.internet_dns, list) and 1 <= len(self.internet_dns) <= 3, "Set 1 to 3 DNS addresses", "INVALID_POLICY")
        for addr in self.internet_dns:
            public_network(addr + "/32")


class Runner:
    def run(self, args, *, timeout=45, check=True, input_text=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DOCKER_", "COMPOSE_"))}
        env.update(LC_ALL="C", COMPOSE_DISABLE_ENV_FILE="1")
        try:
            result = subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, check=False, env=env)
        except FileNotFoundError:
            raise InfraError("DEPENDENCY_MISSING", "Required executable unavailable: " + args[0]) from None
        except subprocess.TimeoutExpired:
            # The daemon may finish work after the client times out: cleanup uses labels.
            raise InfraError("COMMAND_TIMEOUT", "Worker command timed out") from None
        if check and result.returncode:
            # Docker diagnostics can contain supplied environment or health data.
            raise InfraError("COMMAND_FAILED", "Worker command failed: " + args[0] + " " + args[1])
        return result

    def docker(self, *args, **kw):
        return self.run(["docker", "--host", "unix:///var/run/docker.sock", *args], **kw)

    def docker_json(self, *args):
        return json.loads(self.docker(*args).stdout)


def connected_cidrs(runner):
    routes = json.loads(runner.run(["ip", "-j", "-4", "route", "show", "table", "all"]).stdout)
    result = set()
    for route in routes:
        dst = route.get("dst", "default")
        if dst != "default" and not dst.endswith("/0"):
            try:
                result.add(str(ipaddress.IPv4Network(dst, strict=False)))
            except ValueError:
                raise InfraError("HOST_UNSUPPORTED", "Cannot interpret host IPv4 route") from None
    return sorted(result)


def doctor(policy, runner):
    require(platform.system() == "Linux" and os.geteuid() == 0, "Run on the dedicated Linux Docker worker as root", "HOST_UNSUPPORTED")
    for executable in ("docker", "iptables", "ip6tables", "ip"):
        require(shutil.which(executable) is not None, "Missing executable: " + executable, "DEPENDENCY_MISSING")
    require(Path("/var/run/docker.sock").is_socket(), "Local Docker socket is missing", "DEPENDENCY_MISSING")
    info = runner.docker_json("info", "--format", "{{json .}}")
    require(info.get("OSType") == "linux" and "docker desktop" not in info.get("OperatingSystem", "").lower(), "Docker Desktop is not a supported worker", "HOST_UNSUPPORTED")
    require(int(info.get("ServerVersion", "0").split(".")[0]) >= 28, "Docker Engine 28+ is required", "HOST_UNSUPPORTED")
    require(str(info.get("CgroupVersion")) == "2", "cgroup v2 is required", "HOST_UNSUPPORTED")
    for capability in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota"):
        require(info.get(capability) is True, "Docker cannot enforce " + capability, "HOST_UNSUPPORTED")
    security = " ".join(info.get("SecurityOptions", []))
    require("seccomp" in security and "rootless" not in security, "Rootful Docker with seccomp is required", "HOST_UNSUPPORTED")
    # This implementation intentionally supports the Docker iptables backend only.
    runner.run(["iptables", "-w", "5", "-S", "DOCKER-USER"])
    forward = runner.run(["iptables", "-w", "5", "-S", "FORWARD"]).stdout.splitlines()
    rules = [line for line in forward if line.startswith("-A ")]
    require(rules and rules[0] == "-A FORWARD -j DOCKER-USER", "DOCKER-USER must be the first FORWARD hook; nftables backend/custom early ACCEPT is unsupported", "HOST_UNSUPPORTED")
    for knob in ("/proc/sys/net/bridge/bridge-nf-call-iptables", "/proc/sys/net/bridge/bridge-nf-call-ip6tables"):
        require(Path(knob).exists() and Path(knob).read_text().strip() == "1", "Enable bridge netfilter: " + knob, "HOST_UNSUPPORTED")
    require(policy.capacity_memory_mb + policy.host_reserved_memory_mb <= info["MemTotal"] // 1048576, "Worker RAM budget exceeds installed memory minus host reserve", "CAPACITY_EXCEEDED")
    require(policy.capacity_cpu <= max(.1, info["NCPU"] - 1), "Reserve at least one CPU for the host", "CAPACITY_EXCEEDED")
    require(shutil.disk_usage(info["DockerRootDir"]).free // 1048576 >= policy.min_free_disk_mb, "Docker data disk reserve is exhausted", "CAPACITY_EXCEEDED")
    compose_version = runner.docker("compose", "version", "--short").stdout.strip().lstrip("v").split(".")
    require(tuple(int(x) for x in compose_version[:2]) >= (2, 24), "Compose 2.24+ is required", "HOST_UNSUPPORTED")
    return {"engine": info["ServerVersion"], "cgroup": "2", "compose": ".".join(compose_version), "worker": policy.instance}
