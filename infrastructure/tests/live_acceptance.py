"""Real Docker acceptance on a dedicated worker. Exit 77 means BLOCKED, never PASS."""

import argparse
import errno
import ipaddress
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cyberpod_infra.engine import Engine
from cyberpod_infra.host import Policy, Runner, doctor
from cyberpod_infra.model import InfraError, PREFIX, public_network
from examples.make_request import request


class Acceptance:
    def __init__(self, engine, image):
        self.engine, self.image = engine, image
        self.sessions, self.checks = [], []

    def check(self, name, condition):
        self.checks.append({"name": name, "result": "PASS" if condition else "FAIL"})
        if not condition:
            raise AssertionError(name)

    def create(self, mode, allow=()):
        spec = request(image=self.image, internet=mode)
        spec["network"]["allow"] = list(allow)
        self.sessions.append(spec["session_id"])
        started = self.engine.start(spec)
        self.check(mode + ": start accepted", not started["error"] and started["status"] in ("STARTING", "READY"))
        ready = self.engine.wait(spec["session_id"], timeout=60)
        self.check(mode + ": every service ready", ready["status"] == "READY")
        return spec, ready

    def probe(self, container, *args):
        return self.engine.runner.docker("exec", container, "python3", "/probe.py", *args, check=False, timeout=10)

    def python(self, container, source):
        return self.engine.runner.docker("exec", container, "python3", "-c", source, check=False, timeout=15)

    def own_target(self, ready):
        cid = ready["services"]["client"]["container_id"]
        result = self.probe(cid, "http", "http://target:8080/")
        self.check(ready["session_id"] + ": own target by service DNS", result.returncode == 0 and json.loads(result.stdout)["session_id"] == ready["session_id"])

    def isolation(self, a, b):
        # Positive controls in both sessions precede any negative reachability assertion.
        self.own_target(a)
        self.own_target(b)
        self.check("separate subnets", a["network"]["subnet"] != b["network"]["subnet"])
        for source, target in ((a, b), (b, a)):
            for source_name, source_service in source["services"].items():
                for target_name, target_service in target["services"].items():
                    result = self.probe(source_service["container_id"], "tcp", target_service["ip"], "8080")
                    self.check(f'{source["network"]["internet"]}/{source_name} cannot reach other {target_name}', result.returncode != 0)
            target_id = target["services"]["target"]["container_id"]
            target_name = self.engine.inspect("container", [target_id])[0]["Name"].lstrip("/")
            result = self.probe(source["services"]["client"]["container_id"], "dns", target_name)
            self.check("other session container name does not resolve", result.returncode != 0)
        # Prove host service exists, then show neither Lab can initiate access to it.
        network = self.engine.inspect("network", a["resources"]["network"])[0]
        gateway = network["IPAM"]["Config"][0]["Gateway"]
        with socket.socket() as sentinel:
            sentinel.bind((gateway, 0))
            sentinel.listen(16)
            port = sentinel.getsockname()[1]
            with socket.create_connection((gateway, port), timeout=1):
                pass
            for ready in (a, b):
                result = self.probe(ready["services"]["client"]["container_id"], "tcp", gateway, str(port))
                self.check(ready["network"]["internet"] + ": host gateway service blocked", result.returncode != 0)
        # Endpoint metadata is consumed only by a trusted process on this worker.
        for ready in (a, b):
            for endpoint in ready["endpoints"]:
                with socket.create_connection((endpoint["address"], endpoint["port"]), timeout=1):
                    pass
            self.check("worker can reach declared private upstreams", True)

    def stop(self, sid):
        result = self.engine.stop(sid)
        self.check("stop cleans containers, networks and temporary volumes", result["status"] == "CLEANED" and not any(self.engine.discover(sid).values()))
        again = self.engine.stop(sid)
        self.check("repeated stop is safe", again["status"] == "CLEANED")

    def hardening(self, ready):
        cid = ready["services"]["client"]["container_id"]
        result = self.python(cid, "import os; assert os.geteuid()==1000; s=open('/proc/self/status').read(); assert 'NoNewPrivs:\\t1' in s; assert 'CapEff:\\t0000000000000000' in s")
        self.check("non-root, no-new-privileges and zero effective capabilities", result.returncode == 0)
        result = self.python(cid, "import socket; socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)")
        self.check("raw sockets unavailable", result.returncode != 0)
        source = """
import errno, os
try:
    with open('/probe.py', 'a') as f: f.write('x')
except OSError as e:
    assert e.errno in (errno.EROFS, errno.EACCES)
else: raise AssertionError('root filesystem writable')
p='/work/shared/quota-test'
try:
    try:
        with open(p,'wb') as f:
            for i in range(34): f.write(b'x' * 1048576)
    except OSError as e:
        assert e.errno == errno.ENOSPC
    else: raise AssertionError('tmpfs quota not enforced')
finally:
    if os.path.exists(p): os.unlink(p)
"""
        self.check("read-only root and real volume ENOSPC limit", self.python(cid, source).returncode == 0)

    def run(self, args):
        a_spec, a = self.create("none")
        b_spec, b = self.create("none")
        self.isolation(a, b)
        self.hardening(a)
        cid = a["services"]["client"]["container_id"]
        self.check("write restart marker", self.python(cid, "open('/work/shared/restart-marker','w').write('old')").returncode == 0)
        fresh = self.engine.restart(a_spec, a["generation"])
        fresh = self.engine.wait(a_spec["session_id"], timeout=60)
        self.check("restart creates a fresh generation", fresh["generation"] != a["generation"])
        cid = fresh["services"]["client"]["container_id"]
        self.check("restart resets temporary data", self.python(cid, "import os; assert not os.path.exists('/work/shared/restart-marker')").returncode == 0)
        self.stop(a_spec["session_id"])
        self.own_target(b)
        self.check("stopping A preserves B", self.engine.status(b_spec["session_id"])["status"] == "READY")
        self.engine.runner.docker("kill", b["services"]["target"]["container_id"])
        self.check("target crash produces FAILED", self.engine.status(b_spec["session_id"])["status"] == "FAILED")
        self.engine.reap()
        self.check("reaper removes failed session", not any(self.engine.discover(b_spec["session_id"]).values()))

        # Test networks with default routes as well: offline isolation alone is insufficient.
        allow = [{"cidr": args.egress_ip + "/32", "protocol": "tcp", "port": args.allowed_port}] if args.egress_ip else []
        r_spec, restricted = self.create("restricted", allow)
        i_spec, internet = self.create("internet")
        self.isolation(restricted, internet)
        if args.egress_ip:
            i_cid = internet["services"]["client"]["container_id"]
            r_cid = restricted["services"]["client"]["container_id"]
            for port in (args.allowed_port, args.denied_port):
                self.check("positive Internet fixture control on port " + str(port), self.probe(i_cid, "tcp", args.egress_ip, str(port)).returncode == 0)
            self.check("restricted allowed port reachable", self.probe(r_cid, "tcp", args.egress_ip, str(args.allowed_port)).returncode == 0)
            self.check("restricted other listening port blocked", self.probe(r_cid, "tcp", args.egress_ip, str(args.denied_port)).returncode != 0)
            self.check("Internet DNS positive control", self.probe(i_cid, "dns", "example.com").returncode == 0)
            self.check("restricted external DNS disabled", self.probe(r_cid, "dns", "example.com").returncode != 0)
        self.stop(r_spec["session_id"])
        self.stop(i_spec["session_id"])
        if args.egress_ip:
            off_spec, offline = self.create("none")
            cid = offline["services"]["client"]["container_id"]
            self.check("offline public endpoint blocked", self.probe(cid, "tcp", args.egress_ip, str(args.allowed_port)).returncode != 0)
            self.check("offline external DNS disabled", self.probe(cid, "dns", "example.com").returncode != 0)
            self.stop(off_spec["session_id"])

        starting = request(image=self.image)
        starting["services"][0]["healthcheck"].update(test=["CMD", "python3", "-c", "raise SystemExit(1)"], start_period_seconds=60)
        self.sessions.append(starting["session_id"])
        result = self.engine.start(starting)
        self.check("long readiness starts as STARTING", result["status"] == "STARTING")
        self.stop(starting["session_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create and destroy test sessions on this dedicated worker")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--image", default="cyberpod/infra-probe:dev")
    parser.add_argument("--report", default="live-report.json")
    parser.add_argument("--egress-ip", help="Public IPv4 of a test endpoint YOU control, listening on both ports")
    parser.add_argument("--allowed-port", type=int, default=443)
    parser.add_argument("--denied-port", type=int, default=8443)
    args = parser.parse_args()
    report = {"suite": "real Docker lifecycle and isolation", "status": "BLOCKED", "checks": [], "timestamp": int(time.time())}
    harness, code = None, 77
    try:
        if not args.execute:
            raise InfraError("EXECUTION_NOT_REQUESTED", "Use --execute on a dedicated test worker")
        if args.egress_ip:
            public_network(args.egress_ip + "/32")
            if not (1 <= args.allowed_port <= 65535 and 1 <= args.denied_port <= 65535 and args.allowed_port != args.denied_port):
                raise ValueError("Two distinct valid TCP ports are required")
        policy, runner = Policy.load(args.policy), Runner()
        report["host"] = doctor(policy, runner)
        harness = Acceptance(Engine(policy, runner), args.image)
        harness.run(args)
        report["status"] = "PASS" if args.egress_ip else "PARTIAL"
        report["egress"] = "PASS" if args.egress_ip else "NOT_RUN: supply a controlled two-port public endpoint"
        code = 0 if args.egress_ip else 1
    except InfraError as exc:
        report["error"] = {"code": exc.code, "message": str(exc)}
        if harness:
            report["status"], code = "FAIL", 1
    except Exception as exc:
        report["status"], code = "FAIL", 1
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if harness:
            report["checks"] = harness.checks
            report["cleanup"] = []
            for sid in harness.sessions:
                try:
                    status = harness.engine.stop(sid)["status"]
                except Exception:
                    status = "CLEANUP_FAILED"
                report["cleanup"].append({"session_id": sid, "status": status})
                if status != "CLEANED":
                    report["status"], code = "FAIL", 1
        report["passed"] = sum(c["result"] == "PASS" for c in report["checks"])
        report["failed"] = sum(c["result"] == "FAIL" for c in report["checks"])
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
