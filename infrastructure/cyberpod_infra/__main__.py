"""Private JSON CLI for a trusted Core worker; not a student-facing endpoint."""

import argparse
import json
import logging
import sys

from .engine import Engine
from .firewall import Firewall
from .host import Policy, Runner, doctor
from .model import API, InfraError, compose, escape_compose, require, validate_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default="/etc/cyberpod-infra/worker.json")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "render", "doctor", "host-init", "reap"):
        sub.add_parser(action)
    for action in ("start", "restart"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--wait", action="store_true")
        if action == "restart":
            cmd.add_argument("--generation", required=True)
    for action in ("status", "stop"):
        cmd = sub.add_parser(action)
        cmd.add_argument("session_id")
        if action == "stop":
            cmd.add_argument("--generation")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        raw = None
        if args.action in ("start", "restart", "validate", "render"):
            payload = sys.stdin.read(262145)
            require(len(payload) <= 262144, "Request exceeds 256 KiB")
            raw = validate_request(json.loads(payload))
        if args.action == "validate":
            result = {"api_version": API, "valid": True}
        elif args.action == "render":
            policy = Policy.load(args.policy)
            document = compose(raw, policy.instance, "render-preview")
            if raw["network"]["internet"] == "internet":
                for service in document["services"].values():
                    service["dns"] = policy.internet_dns
            result = escape_compose(document)
        elif args.action in ("doctor", "host-init"):
            policy, runner = Policy.load(args.policy), Runner()
            result = doctor(policy, runner)
            firewall = Firewall(runner)
            if args.action == "host-init":
                # Serialize with starts, stops and the reaper.
                with Engine(policy, runner).lock():
                    firewall.install()
            firewall.audit()
            result["guard"] = "installed"
        else:
            engine = Engine(Policy.load(args.policy))
            if args.action == "start":
                result = engine.start(raw)
            elif args.action == "restart":
                result = engine.restart(raw, args.generation)
            elif args.action == "status":
                result = engine.status(args.session_id)
            elif args.action == "stop":
                result = engine.stop(args.session_id, args.generation)
            else:
                result = engine.reap()
            if getattr(args, "wait", False) and result.get("status") == "STARTING":
                result = engine.wait(raw["session_id"], timeout=raw["startup_timeout_seconds"] + 10)
        print(json.dumps(result, sort_keys=True))
        failed = result.get("status") in ("FAILED", "CLEANUP_FAILED") or result.get("error")
        failed = failed or any(s.get("status") == "CLEANUP_FAILED" for s in result.get("sessions", []))
        return 1 if failed else 0
    except (InfraError, ValueError, TypeError) as exc:
        code = exc.code if isinstance(exc, InfraError) else "INVALID_REQUEST"
        message = str(exc) if isinstance(exc, InfraError) else "Malformed request or policy"
        print(json.dumps({"api_version": API, "error": {"code": code, "message": message}}))
        return 2
    except Exception:
        # Never return tracebacks or subprocess stderr containing secrets to Core.
        print(json.dumps({"api_version": API, "error": {"code": "INTERNAL_ERROR", "message": "Unexpected worker failure; inspect worker state and reconcile resources"}}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
