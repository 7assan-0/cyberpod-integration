"""Host policy for dedicated cp* bridges; never flush Docker's own chains."""

import shlex
import re

from .model import BLOCKED, InfraError, require

OUT = "CPINFRA-OUT"
FWD = "CPINFRA-FWD"
HOST = "CPINFRA-HOST"
V6 = "CPINFRA-V6"


def canonical_rule(rule):
    # iptables-save omits /32 on individual IPv4 addresses.
    return [token[:-3] if i and rule[i - 1] in ("-s", "-d") and token.endswith("/32") else token for i, token in enumerate(rule)]


def session_rules(bridge, subnet, internet, allow, protected):
    rules = [
        ["!", "-s", subnet, "-j", "DROP"],
        ["-o", bridge, "-d", subnet, "-j", "ACCEPT"],
        ["-o", "cp+", "-j", "DROP"],
        ["-m", "addrtype", "--dst-type", "LOCAL", "-j", "DROP"],
    ]
    for cidr in sorted(set(BLOCKED) | set(protected)):
        rules.append(["-d", cidr, "-j", "DROP"])
    if internet == "internet":
        rules.append(["-j", "ACCEPT"])
    elif internet == "restricted":
        for rule in allow:
            rules.append(["-d", rule["cidr"], "-p", rule["protocol"], "-m", rule["protocol"], "--dport", str(rule["port"]), "-j", "ACCEPT"])
    rules.append(["-j", "DROP"])
    return rules


class Firewall:
    def __init__(self, runner):
        self.runner = runner

    def cmd(self, *args, family="iptables", check=True):
        return self.runner.run([family, "-w", "5", *args], check=check)

    def rules(self, chain, family="iptables"):
        result = self.cmd("-S", chain, family=family, check=False)
        if result.returncode:
            return None
        return [canonical_rule(shlex.split(line)[2:]) for line in result.stdout.splitlines() if line.startswith("-A ")]

    def ensure_chain(self, chain, rules, family="iptables"):
        existing = self.rules(chain, family)
        if existing is None:
            self.cmd("-N", chain, family=family)
            for rule in rules:
                self.cmd("-A", chain, *rule, family=family)
        else:
            require(existing == [canonical_rule(r) for r in rules], "Firewall chain differs from expected policy: " + chain, "FIREWALL_DRIFT")

    def hook(self, parent, chain, family="iptables"):
        rules = self.rules(parent, family)
        require(rules is not None, "Required firewall chain is absent", "HOST_UNSUPPORTED")
        rule = ["-j", chain]
        if rule not in rules:
            self.cmd("-I", parent, "1", *rule, family=family)
        else:
            require(rules[0] == rule, "CyberPod firewall hook must be first", "FIREWALL_DRIFT")

    def audit_dispatch(self):
        rules = self.rules(OUT) or []
        require(rules[-1:] == [["-j", "DROP"]], "Missing default deny", "FIREWALL_DRIFT")
        seen = set()
        for rule in rules[:-1]:
            valid = len(rule) == 4 and rule[0] == "-i" and rule[2] == "-j" and re.fullmatch(r"cp[0-9a-f]{12}", rule[1]) and rule[3] == "CPS" + rule[1][2:] and rule[1] not in seen
            require(valid, "Unexpected session dispatch rule", "FIREWALL_DRIFT")
            seen.add(rule[1])

    @staticmethod
    def base_rules():
        return {
            FWD: [["-i", "cp+", "-j", OUT],
                  ["-o", "cp+", "-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY", "-j", "ACCEPT"],
                  ["-o", "cp+", "-j", "DROP"], ["-j", "RETURN"]],
            HOST: [["-i", "cp+", "-m", "conntrack", "--ctstate", "ESTABLISHED", "--ctdir", "REPLY", "-j", "ACCEPT"],
                   ["-i", "cp+", "-j", "DROP"], ["-j", "RETURN"]],
        }

    def install(self):
        # Build chains completely before making them reachable from hooks.
        if self.rules(OUT) is None:
            self.ensure_chain(OUT, [["-j", "DROP"]])
        else:
            self.audit_dispatch()
        for chain, rules in self.base_rules().items():
            self.ensure_chain(chain, rules)
        self.ensure_chain(V6, [["-i", "cp+", "-j", "DROP"], ["-o", "cp+", "-j", "DROP"], ["-j", "RETURN"]], "ip6tables")
        self.hook("DOCKER-USER", FWD)
        self.hook("INPUT", HOST)
        self.hook("FORWARD", V6, "ip6tables")
        self.hook("INPUT", V6, "ip6tables")

    def audit(self, state=None):
        require((self.rules("FORWARD") or [])[:1] == [["-j", "DOCKER-USER"]], "Docker FORWARD guard is bypassed", "FIREWALL_DRIFT")
        for family, parent, chain in (("iptables", "DOCKER-USER", FWD), ("iptables", "INPUT", HOST), ("ip6tables", "FORWARD", V6), ("ip6tables", "INPUT", V6)):
            require((self.rules(parent, family) or [])[:1] == [["-j", chain]], "Firewall hook is missing or reordered", "FIREWALL_DRIFT")
        for chain, rules in self.base_rules().items():
            require(self.rules(chain) == rules, "Host firewall policy changed", "FIREWALL_DRIFT")
        require(self.rules(V6, "ip6tables") == [["-i", "cp+", "-j", "DROP"], ["-o", "cp+", "-j", "DROP"], ["-j", "RETURN"]], "IPv6 guard changed", "FIREWALL_DRIFT")
        self.audit_dispatch()
        if state:
            n = state["names"]
            require(self.rules(n["chain"]) == [canonical_rule(r) for r in state["firewall_rules"]], "Session egress policy changed", "FIREWALL_DRIFT")
            require(["-i", n["bridge"], "-j", n["chain"]] in (self.rules(OUT) or []), "Session hook is missing", "FIREWALL_DRIFT")

    def add(self, state):
        n = state["names"]
        self.ensure_chain(n["chain"], state["firewall_rules"])
        rule = ["-i", n["bridge"], "-j", n["chain"]]
        if rule not in (self.rules(OUT) or []):
            self.cmd("-I", OUT, "1", *rule)

    def remove(self, n):
        rule = ["-i", n["bridge"], "-j", n["chain"]]
        while rule in (self.rules(OUT) or []):
            self.cmd("-D", OUT, *rule)
        if self.rules(n["chain"]) is not None:
            self.cmd("-F", n["chain"])
            self.cmd("-X", n["chain"])
