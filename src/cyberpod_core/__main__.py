import argparse
import importlib
import os
import secrets
from pathlib import Path

from aiohttp import web

from .api import create_app
from .auth import StaticTokens
from .demo_runtime import MemoryRuntime
from .engine import Engine
from .logging_config import configure_logging
from .models import Principal
from .registry import LabRegistry
from .repository import SQLiteRepository


def load_factory(value):
    module, separator, attribute = value.partition(":")
    if not separator:
        raise ValueError("Factory must be a trusted module:callable")
    return getattr(importlib.import_module(module), attribute)()


def main():
    parser = argparse.ArgumentParser(description="CyberPod Core; use --demo for simulated API acceptance only")
    parser.add_argument("--labs", type=Path, default=Path("labs"))
    parser.add_argument("--database", type=Path, default=Path("var/core.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--runtime-factory", help="Astra #2 trusted module:factory returning Runtime")
    parser.add_argument("--validators-factory", help="Astra #6 trusted module:factory returning dict[id, Validator]")
    parser.add_argument("--access-factory", help="Trusted module:factory returning AccessBroker")
    parser.add_argument("--tokens", type=Path, help="Private JSON bearer-token file (required outside --demo)")
    args = parser.parse_args()
    if args.demo and (args.runtime_factory or args.tokens or args.host not in {"127.0.0.1", "::1", "localhost"}):
        parser.error("--demo requires loopback and cannot be combined with --runtime-factory or --tokens")
    if not args.demo and (not args.runtime_factory or not args.tokens):
        parser.error("Configure --runtime-factory and --tokens, or explicitly select --demo")
    configure_logging()
    if args.demo:
        runtime = MemoryRuntime()
        token = os.environ.get("CYBERPOD_DEMO_TOKEN") or secrets.token_urlsafe(32)
        auth = StaticTokens({token: Principal(subject="demo-student", scopes={"student", "admin"})})
        print("SIMULATED MODE: no containers, isolated networks or desktops are launched.", flush=True)
        print(f"Local demo bearer token: {token}", flush=True)
    else:
        runtime = load_factory(args.runtime_factory)
        if runtime.simulated:
            parser.error("A simulated runtime requires explicit --demo")
        auth = StaticTokens.from_file(args.tokens)
    registry = LabRegistry(args.labs)
    registry.reload()
    validators = load_factory(args.validators_factory) if args.validators_factory else {}
    broker = load_factory(args.access_factory) if args.access_factory else None
    repo = SQLiteRepository(args.database)
    engine = Engine(registry, repo, runtime, validators, broker)
    app = create_app(engine, auth)
    # Background lifecycle workers are owned by Engine, independent of client disconnect.
    web.run_app(app, host=args.host, port=args.port, access_log=None, handler_cancellation=True)


if __name__ == "__main__":
    main()

