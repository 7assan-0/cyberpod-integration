import argparse
import json
from pathlib import Path
from aiohttp import web
from cyberpod_core.__main__ import load_factory
from cyberpod_core.demo_runtime import MemoryRuntime
from cyberpod_core.engine import Engine
from cyberpod_core.hardening import apply_hardening
from cyberpod_core.models import Principal
from cyberpod_core.registry import LabRegistry
from cyberpod_core.repository import SQLiteRepository
from cyberpod_core.session_secrets import SessionVault
from .student_api import USERS, create_student_app
from .validation import ScoreValidator


def main():
    parser = argparse.ArgumentParser(description='CyberPod student /api/v1 server')
    parser.add_argument('--demo', action='store_true', help='Local API simulation; no Kali containers')
    parser.add_argument('--labs', type=Path, default=Path('labs'))
    parser.add_argument('--database', type=Path, default=Path('var/student.sqlite3'))
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--users', type=Path, help='Private JSON mapping of email to password')
    parser.add_argument('--runtime-factory')
    parser.add_argument('--validators-factory')
    parser.add_argument('--access-factory')
    parser.add_argument('--trusted-proxy', action='append', default=[], help='Explicit trusted proxy IP; repeat for multiple proxies')
    args = parser.parse_args()
    if args.demo and (args.host not in {'127.0.0.1', '::1', 'localhost'} or args.runtime_factory):
        parser.error('--demo requires loopback and cannot use a runtime factory')
    if not args.demo and not (args.users and args.runtime_factory and args.validators_factory):
        parser.error('Configure --users, --runtime-factory and --validators-factory, or use --demo')
    users = USERS if args.demo and not args.users else {
        email: (password, Principal(subject=email.strip().lower())) for email, password in json.loads(args.users.read_text()).items()
    }
    vault = SessionVault()
    runtime = MemoryRuntime() if args.demo else load_factory(args.runtime_factory)
    if not args.demo and runtime.simulated:
        parser.error('A simulated runtime requires --demo')
    registry = LabRegistry(args.labs)
    registry.reload()
    validators = load_factory(args.validators_factory) if args.validators_factory else {
        lab.validator.id: ScoreValidator(vault) for lab in registry.all() if lab.validator
    }
    broker = load_factory(args.access_factory) if args.access_factory else None
    repo = SQLiteRepository(args.database)
    engine = Engine(registry, repo, runtime, validators, broker)
    app = create_student_app(engine, users=users, vault=vault, gateway=getattr(broker, 'gateway', None),
                             secure_cookies=not args.demo, manage_engine=True)
    apply_hardening(app, require_https=not args.demo, trusted_proxies=args.trusted_proxy)
    print('API simulation: no containers or Kali desktop.' if args.demo else 'Infrastructure mode', flush=True)
    web.run_app(app, host=args.host, port=args.port, access_log=None)

if __name__ == '__main__':
    main()
