import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from cyberpod_infra.engine import Engine
from cyberpod_infra.host import Policy
from cyberpod_infra.model import InfraError, PREFIX
from examples.make_request import request


class FakeFirewall:
    def __init__(self):
        self.sessions = set()
        self.broken = False

    def install(self):
        pass

    def audit(self, state=None):
        if self.broken:
            raise InfraError("FIREWALL_DRIFT", "Test drift")
        if state and state["names"]["chain"] not in self.sessions:
            raise InfraError("FIREWALL_DRIFT", "Test missing chain")

    def add(self, state):
        self.sessions.add(state["names"]["chain"])

    def remove(self, names):
        self.sessions.discard(names["chain"])


class FakeDocker:
    """In-memory daemon substitute for failure injection; never claims network isolation."""
    def __init__(self):
        self.data = {"container": {}, "network": {}, "volume": {}}
        self.calls = []
        self.sequence = 0
        self.fail_compose_start = False
        self.timeout_compose_start = False
        self.fail_network_delete = False
        self.image_volumes = None

    @staticmethod
    def flags(args, key):
        return [args[i + 1] for i in range(len(args) - 1) if args[i] == key]

    @staticmethod
    def tags(args):
        return dict(value.split("=", 1) for value in FakeDocker.flags(args, "--label"))

    def newid(self):
        self.sequence += 1
        return f"{self.sequence:064x}"

    def docker_json(self, *args):
        return json.loads(self.docker(*args).stdout)

    def docker(self, *args, **kw):
        args = list(args)
        self.calls.append(args)
        output = ""
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "a" * 64, "Size": 1024, "Config": {"Volumes": self.image_volumes}}])
        elif args[0] == "compose":
            path = self.flags(args, "--file")[0]
            project = self.flags(args, "--project-name")[0]
            document = json.loads(Path(path).read_text().replace("$$", "$"))
            if "create" in args:
                network_name = document["networks"]["lab"]["name"]
                for name, s in document["services"].items():
                    rid = self.newid()
                    volumes = []
                    for m in s.get("volumes", []):
                        volumes.append({"Type": "volume", "Name": document["volumes"][m["source"]]["name"]})
                    self.data["container"][rid] = {
                        "Id": rid, "project": project,
                        "Config": {"Labels": s["labels"], "User": s["user"], "Healthcheck": {"Test": s["healthcheck"]["test"]}},
                        "HostConfig": {"ReadonlyRootfs": s["read_only"], "Privileged": s["privileged"], "CapDrop": s["cap_drop"], "SecurityOpt": s["security_opt"],
                                       "CgroupnsMode": s["cgroup"], "IpcMode": s["ipc"], "PidMode": "", "ShmSize": int(s["shm_size"][:-1]) * 1048576,
                                       "Memory": int(s["mem_limit"][:-1]) * 1048576, "MemorySwap": int(s["memswap_limit"][:-1]) * 1048576,
                                       "NanoCpus": int(s["cpus"] * 1e9), "PidsLimit": s["pids_limit"]},
                        "State": {"Running": False, "Health": {"Status": "starting"}},
                        "NetworkSettings": {"Networks": {network_name: {"IPAddress": ""}}}, "Mounts": volumes,
                    }
            elif "start" in args:
                for rid, item in self.data["container"].items():
                    if item["project"] == project:
                        item["State"]["Running"] = True
                        for net in item["NetworkSettings"]["Networks"].values():
                            net["IPAddress"] = "10.240.0." + str(int(rid, 16) + 1)
                if self.fail_compose_start:
                    raise InfraError("COMMAND_FAILED", "Injected partial start failure")
                if self.timeout_compose_start:
                    raise InfraError("COMMAND_TIMEOUT", "Injected daemon operation still in flight")
            else:
                raise AssertionError(args)
        else:
            kind, operation = args[:2]
            if operation == "ls":
                filters = self.flags(args, "--filter")
                ids = []
                for rid, item in self.data[kind].items():
                    tags = item.get("Config", {}).get("Labels", {}) if kind == "container" else item.get("Labels", {})
                    wanted = dict(f[len("label="):].split("=", 1) for f in filters)
                    if all(tags.get(k) == v for k, v in wanted.items()):
                        ids.append(rid)
                output = "\n".join(ids)
            elif operation == "inspect":
                output = json.dumps([self.data[kind][rid] for rid in args[2:]])
            elif operation == "create":
                rid = args[-1] if kind == "volume" else self.newid()
                item = {"Id": rid, "Name": args[-1], "Labels": self.tags(args)}
                if kind == "network":
                    item["IPAM"] = {"Config": [{"Subnet": self.flags(args, "--subnet")[0]}]}
                    item["Internal"] = "--internal" in args
                    item["Options"] = dict(x.split("=", 1) for x in self.flags(args, "--opt"))
                else:
                    item["Driver"] = "local"
                    item["Options"] = dict(x.split("=", 1) for x in self.flags(args, "--opt"))
                self.data[kind][rid] = item
                output = rid
            elif operation == "stop":
                self.data[kind][args[-1]]["State"]["Running"] = False
            elif operation == "rm":
                if kind == "network" and self.fail_network_delete:
                    raise InfraError("COMMAND_FAILED", "Injected network busy")
                self.data[kind].pop(args[-1], None)
            else:
                raise AssertionError(args)
        return SimpleNamespace(stdout=output, stderr="", returncode=0)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [1800000000]
        self.runner, self.firewall = FakeDocker(), FakeFirewall()
        self.policy = Policy(state_dir=self.tmp.name, allowed_images=["cyberpod/infra-probe:dev"], require_digest=False)
        self.engine = self.make_engine()

    def make_engine(self):
        return Engine(self.policy, self.runner, self.firewall, clock=lambda: self.clock[0], preflight=lambda *_: {}, routes=lambda _: [])

    def spec(self):
        raw = request()
        raw["expires_at"] = self.clock[0] + 600
        return raw

    def healthy(self, sid):
        for item in self.runner.data["container"].values():
            if item["Config"]["Labels"][f"{PREFIX}.session"] == sid:
                item["State"]["Health"]["Status"] = "healthy"

    def test_full_lifecycle_and_idempotent_stop(self):
        spec = self.spec()
        starting = self.engine.start(spec)
        self.assertEqual(starting["status"], "STARTING")
        self.assertEqual(starting["endpoints"], [])
        self.healthy(spec["session_id"])
        ready = self.engine.status(spec["session_id"])
        self.assertEqual(ready["status"], "READY")
        self.assertEqual(len(ready["endpoints"]), 2)
        self.assertEqual(self.engine.stop(spec["session_id"])["status"], "CLEANED")
        self.assertEqual(self.engine.stop(spec["session_id"])["status"], "CLEANED")
        self.assertFalse(any(self.runner.data.values()))
        self.assertFalse(self.firewall.sessions)
        self.assertFalse(list(Path(self.tmp.name).glob("*.compose")))

    def test_duplicate_start_does_not_recreate_and_changed_spec_conflicts(self):
        spec = self.spec()
        first = self.engine.start(spec)
        second = self.engine.start(spec)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(len(self.runner.data["container"]), 2)
        spec["lab_id"] = "other-lab"
        with self.assertRaisesRegex(InfraError, "different request"):
            self.engine.start(spec)

    def test_two_sessions_get_separate_network_volume_and_resources(self):
        a, b = self.spec(), self.spec()
        ra, rb = self.engine.start(a), self.engine.start(b)
        self.assertNotEqual(ra["network"]["name"], rb["network"]["name"])
        self.assertNotEqual(ra["network"]["subnet"], rb["network"]["subnet"])
        self.assertTrue(set(ra["resources"]["volume"]).isdisjoint(rb["resources"]["volume"]))
        self.engine.stop(a["session_id"])
        self.assertEqual(self.engine.discover(b["session_id"]), rb["resources"])

    def test_failure_after_partial_start_rolls_back_all_resources(self):
        self.runner.fail_compose_start = True
        result = self.engine.start(self.spec())
        self.assertEqual(result["status"], "CLEANED")
        self.assertEqual(result["error"], "COMMAND_FAILED")
        self.assertFalse(any(self.runner.data.values()))

    def test_cleanup_failure_retains_budget_and_retry_succeeds(self):
        self.policy.max_sessions = 1
        spec = self.spec()
        self.engine.start(spec)
        self.runner.fail_network_delete = True
        self.assertEqual(self.engine.stop(spec["session_id"])["status"], "CLEANUP_FAILED")
        self.assertTrue(self.firewall.sessions)
        with self.assertRaisesRegex(InfraError, "capacity"):
            self.engine.start(self.spec())
        self.runner.fail_network_delete = False
        self.assertEqual(self.engine.stop(spec["session_id"])["status"], "CLEANED")
        self.assertEqual(self.engine.start(self.spec())["status"], "STARTING")

    def test_daemon_timeout_retains_capacity_during_reconciliation_window(self):
        self.policy.max_sessions = 1
        self.runner.timeout_compose_start = True
        spec = self.spec()
        result = self.engine.start(spec)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertTrue(self.firewall.sessions)
        with self.assertRaisesRegex(InfraError, "capacity"):
            self.engine.start(self.spec())
        self.clock[0] += 301
        self.engine.reap()
        self.assertEqual(self.engine.status(spec["session_id"])["status"], "CLEANED")

    def test_running_unhealthy_and_missing_health_never_ready(self):
        for health in ("unhealthy", None):
            spec = self.spec()
            self.engine.start(spec)
            self.healthy(spec["session_id"])
            cid = self.engine.discover(spec["session_id"])["container"][0]
            if health:
                self.runner.data["container"][cid]["State"]["Health"]["Status"] = health
            else:
                self.runner.data["container"][cid]["State"].pop("Health")
            self.assertEqual(self.engine.status(spec["session_id"])["status"], "FAILED")
            self.engine.stop(spec["session_id"])

    def test_start_timeout_reaped_without_waiting_until_ttl(self):
        spec = self.spec()
        self.engine.start(spec)
        self.clock[0] += 46
        result = self.engine.reap()
        self.assertEqual(result["sessions"][0]["status"], "CLEANED")
        self.assertEqual(result["sessions"][0]["error"], "STARTUP_TIMEOUT")

    def test_expiration_removes_resources(self):
        spec = self.spec()
        self.engine.start(spec)
        self.clock[0] += 601
        result = self.engine.status(spec["session_id"])
        self.assertEqual(result["status"], "CLEANED")
        self.assertEqual(result["error"], "SESSION_EXPIRED")

    def test_restart_gets_new_generation_and_stale_stop_rejected(self):
        spec = self.spec()
        first = self.engine.start(spec)
        second = self.engine.restart(spec, first["generation"])
        self.assertNotEqual(first["generation"], second["generation"])
        with self.assertRaisesRegex(InfraError, "Stale"):
            self.engine.stop(spec["session_id"], first["generation"])
        self.assertEqual(len(self.runner.data["container"]), 2)

    def test_stop_during_starting_is_supported(self):
        spec = self.spec()
        self.assertEqual(self.engine.start(spec)["status"], "STARTING")
        self.assertEqual(self.engine.stop(spec["session_id"])["status"], "CLEANED")

    def test_persistent_state_across_controller_restart(self):
        spec = self.spec()
        original = self.engine.start(spec)
        new_process = self.make_engine()
        recovered = new_process.start(spec)
        self.assertEqual(original["generation"], recovered["generation"])
        self.assertEqual(new_process.stop(spec["session_id"])["status"], "CLEANED")

    def test_unknown_orphan_blocks_admission_until_reconciled(self):
        spec = self.spec()
        self.engine.start(spec)
        self.engine.path(spec["session_id"]).unlink()
        with self.assertRaisesRegex(InfraError, "Orphan"):
            self.engine.start(self.spec())
        self.engine.reap()
        self.assertFalse(any(self.runner.data.values()))
        self.assertEqual(self.engine.start(self.spec())["status"], "STARTING")

    def test_unrelated_host_resources_survive_cleanup(self):
        spec = self.spec()
        self.engine.start(spec)
        for kind in self.runner.data:
            self.runner.data[kind]["unrelated"] = {"Id": "unrelated", "Config": {"Labels": {}}, "Labels": {}, "IPAM": {"Config": []}}
        self.engine.stop(spec["session_id"])
        self.assertTrue(all(list(items) == ["unrelated"] for items in self.runner.data.values()))

    def test_firewall_drift_triggers_cleanup(self):
        spec = self.spec()
        self.engine.start(spec)
        self.firewall.broken = True
        result = self.engine.status(spec["session_id"])
        self.assertEqual(result["status"], "CLEANED")
        self.assertEqual(result["error"], "FIREWALL_DRIFT")
        self.assertFalse(any(self.runner.data.values()))

    def test_image_with_implicit_volume_is_rejected_before_allocation(self):
        self.runner.image_volumes = {"/unbounded": {}}
        with self.assertRaisesRegex(InfraError, "VOLUME"):
            self.engine.start(self.spec())
        self.assertFalse(any(self.runner.data.values()))

    def test_image_not_allowlisted_rejected(self):
        spec = self.spec()
        spec["services"][0]["image"] = "arbitrary:latest"
        with self.assertRaisesRegex(InfraError, "allowlist"):
            self.engine.start(spec)

    def test_concurrent_starts_respect_worker_admission(self):
        self.policy.max_sessions = 1
        outcomes = []
        def run():
            try:
                outcomes.append(self.make_engine().start(self.spec())["status"])
            except InfraError as exc:
                outcomes.append(exc.code)
        a, b = threading.Thread(target=run), threading.Thread(target=run)
        a.start()
        b.start()
        a.join()
        b.join()
        self.assertCountEqual(outcomes, ["STARTING", "CAPACITY_EXCEEDED"])
        self.assertEqual(len(self.runner.data["container"]), 2)

    def test_status_never_discloses_environment_or_health_output(self):
        spec = self.spec()
        spec["services"][0]["environment"] = {"TRAINING_SECRET": "never-print-me"}
        result = self.engine.start(spec)
        self.assertNotIn("never-print-me", json.dumps(result))
        self.assertNotIn("never-print-me", self.engine.path(spec["session_id"]).read_text())
        self.engine.stop(spec["session_id"])
        self.assertFalse(list(Path(self.tmp.name).glob("*.compose")))
