import copy
import ipaddress
import unittest

from cyberpod_infra.firewall import Firewall, canonical_rule, session_rules
from cyberpod_infra.model import InfraError, budget, compose, escape_compose, names, validate_request
from examples.make_request import request


class ContractTests(unittest.TestCase):
    def test_second_lab_uses_identical_adapter(self):
        raw = request()
        raw["lab_id"] = "second-independent-lab"
        spec = validate_request(raw)
        output = compose(spec, "test", "gen")
        self.assertEqual(output["services"]["client"]["environment"]["CYBERPOD_LAB_ID"], raw["lab_id"])

    def test_defaults_are_hardened_and_limits_are_engine_limits(self):
        spec = validate_request(request())
        service = compose(spec, "test", "gen")["services"]["client"]
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertFalse(service["privileged"])
        self.assertTrue(service["read_only"])
        self.assertEqual(service["mem_limit"], service["memswap_limit"])
        self.assertNotIn("ports", service)
        self.assertEqual(service["dns"], ["127.0.0.1"])
        self.assertEqual(service["restart"], "no")

    def test_unknown_docker_options_are_rejected(self):
        for key, value in (("privileged", True), ("network_mode", "host"), ("cap_add", ["SYS_ADMIN"]), ("devices", ["/dev/kvm"]), ("ports", ["8080:8080"]), ("security_opt", ["seccomp=unconfined"])):
            with self.subTest(key=key):
                raw = request()
                raw["services"][0][key] = value
                with self.assertRaises(InfraError):
                    validate_request(raw)

    def test_root_and_mount_escape_rejected(self):
        for user in ("0:0", "root", "1000:0", "100000:1000"):
            raw = request()
            raw["services"][0]["user"] = user
            with self.subTest(user=user), self.assertRaises(InfraError):
                validate_request(raw)
        for path in ("/", "/etc", "/proc", "/sys", "/var/run/docker.sock", "/home/../../etc", "/home//learner"):
            raw = request()
            raw["services"][0]["tmpfs"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(InfraError):
                validate_request(raw)

    def test_duplicate_or_nested_mounts_rejected(self):
        for path in ("/tmp", "/tmp/child"):
            raw = request()
            raw["services"][0]["tmpfs"].append({"path": path, "size_mb": 1})
            with self.assertRaises(InfraError):
                validate_request(raw)

    def test_bad_identity_and_numeric_values_rejected(self):
        raw = request("../../etc/passwd")
        with self.assertRaises(InfraError):
            validate_request(raw)
        for value in (True, float("nan"), float("inf"), -1, 0, 100):
            raw = request()
            raw["services"][0]["resources"]["cpus"] = value
            with self.subTest(value=value), self.assertRaises(InfraError):
                validate_request(raw)

    def test_healthy_required_not_just_running(self):
        raw = request()
        del raw["services"][0]["healthcheck"]
        with self.assertRaises(InfraError):
            validate_request(raw)
        raw = request()
        raw["services"][0]["healthcheck"]["test"] = ["CMD-SHELL", "true"]
        with self.assertRaises(InfraError):
            validate_request(raw)

    def test_no_env_metadata_spoofing(self):
        raw = request()
        raw["services"][0]["environment"] = {"CYBERPOD_SESSION_ID": "someone-else"}
        with self.assertRaises(InfraError):
            validate_request(raw)

    def test_compose_interpolation_cannot_read_host_environment(self):
        raw = request()
        raw["services"][0]["environment"] = {"TOKEN": "literal-${HOME}-$abc-$(id)-$$"}
        document = escape_compose(compose(validate_request(raw), "test", "gen"))
        self.assertEqual(document["services"]["client"]["environment"]["TOKEN"], "literal-$${HOME}-$$abc-$$(id)-$$$$")

    def test_storage_budget_and_unused_volume_rejected(self):
        raw = request()
        raw["volumes"][0]["size_mb"] = 200
        with self.assertRaises(InfraError):
            validate_request(raw)
        raw = request()
        raw["services"][0]["volumes"] = []
        with self.assertRaises(InfraError):
            validate_request(raw)

    def test_shared_volume_reserved_once(self):
        raw = request()
        raw["services"][1]["volumes"] = copy.deepcopy(raw["services"][0]["volumes"])
        self.assertEqual(budget(validate_request(raw))["storage_mb"], 96)

    def test_session_names_are_unique_and_linux_interface_safe(self):
        a, b = request(), request()
        self.assertNotEqual(names("worker-a", a["session_id"]), names("worker-a", b["session_id"]))
        self.assertNotEqual(names("worker-a", a["session_id"]), names("worker-b", a["session_id"]))
        self.assertLessEqual(len(names("worker-a", a["session_id"])["bridge"]), 15)

    def test_egress_contract_never_allows_private_or_catchall(self):
        for cidr in ("0.0.0.0/0", "10.1.0.0/16", "169.254.169.254/32", "127.0.0.1/32", "::/0", "8.8.8.0/8"):
            raw = request(internet="restricted")
            raw["network"]["allow"] = [{"cidr": cidr, "protocol": "tcp", "port": 443}]
            with self.subTest(cidr=cidr), self.assertRaises(InfraError):
                validate_request(raw)


def verdict(rules, *, source="10.240.0.2", destination, outgoing="eth0", protocol="tcp", port=443, local=False):
    """Small packet-policy evaluator for the supported rule subset, not a network test."""
    for rule in rules:
        match, negate, i = True, False, 0
        while i < len(rule):
            key = rule[i]
            if key == "!":
                negate, i = True, i + 1
                continue
            value = rule[i + 1]
            condition = True
            if key == "-s":
                condition = ipaddress.ip_address(source) in ipaddress.ip_network(value)
            elif key == "-d":
                condition = ipaddress.ip_address(destination) in ipaddress.ip_network(value)
            elif key == "-o":
                condition = outgoing.startswith(value[:-1]) if value.endswith("+") else outgoing == value
            elif key == "-p":
                condition = protocol == value
            elif key == "--dport":
                condition = port == int(value)
            elif key == "--dst-type":
                condition = local
            elif key == "-j":
                if match:
                    return value
                break
            elif key != "-m":
                raise AssertionError("Unsupported rule token: " + key)
            match = match and (not condition if negate else condition)
            negate, i = False, i + 2
    return "RETURN"


class EgressPolicyTests(unittest.TestCase):
    def test_iptables_single_host_cidr_normalization(self):
        self.assertEqual(canonical_rule(["-d", "8.8.8.8/32", "-j", "DROP"]), ["-d", "8.8.8.8", "-j", "DROP"])

    def test_dispatch_rejects_early_accept_wrong_chain_and_duplicates(self):
        firewall = Firewall(None)
        for rules in (
            [["-j", "ACCEPT"], ["-j", "DROP"]],
            [["-i", "cpaaaaaaaaaaaa", "-j", "CPSbbbbbbbbbbbb"], ["-j", "DROP"]],
            [["-i", "cpaaaaaaaaaaaa", "-j", "CPSaaaaaaaaaaaa"]] * 2 + [["-j", "DROP"]],
        ):
            firewall.rules = lambda _, rules=rules: rules
            with self.assertRaises(InfraError):
                firewall.audit_dispatch()

    def rules(self, mode, allow=(), protected=()):
        return session_rules("cpaaaaaaaaaaaa", "10.240.0.0/28", mode, allow, protected)

    def test_same_session_allowed_in_all_modes(self):
        for mode in ("none", "restricted", "internet"):
            self.assertEqual(verdict(self.rules(mode), destination="10.240.0.3", outgoing="cpaaaaaaaaaaaa"), "ACCEPT")

    def test_cross_session_blocked_even_with_public_address_and_internet(self):
        for mode in ("none", "restricted", "internet"):
            for target in ("10.240.0.18", "8.8.8.8"):
                self.assertEqual(verdict(self.rules(mode), destination=target, outgoing="cpbbbbbbbbbbbb"), "DROP")

    def test_host_private_metadata_and_spoofed_sources_blocked(self):
        rules = self.rules("internet", protected=["11.20.0.0/16"])
        for addr in ("127.0.0.1", "169.254.169.254", "172.17.0.1", "10.240.0.1", "100.64.1.1", "192.168.1.1", "11.20.0.1"):
            self.assertEqual(verdict(rules, destination=addr), "DROP")
        self.assertEqual(verdict(rules, destination="8.8.8.8", local=True), "DROP")
        self.assertEqual(verdict(rules, destination="8.8.8.8", source="1.2.3.4"), "DROP")

    def test_restricted_is_exact_cidr_protocol_port(self):
        rules = self.rules("restricted", [{"cidr": "8.8.8.8/32", "protocol": "tcp", "port": 443}])
        self.assertEqual(verdict(rules, destination="8.8.8.8"), "ACCEPT")
        for overrides in ({"port": 80}, {"protocol": "udp"}, {"destination": "8.8.4.4"}):
            fields = dict(destination="8.8.8.8")
            fields.update(overrides)
            self.assertEqual(verdict(rules, **fields), "DROP")

    def test_offline_denies_public_internet(self):
        self.assertEqual(verdict(self.rules("none"), destination="8.8.8.8"), "DROP")
        self.assertEqual(verdict(self.rules("internet"), destination="8.8.8.8"), "ACCEPT")
