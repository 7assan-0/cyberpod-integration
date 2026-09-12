import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

from cyberpod_core.demo_runtime import MemoryRuntime
from cyberpod_core.engine import Engine
from cyberpod_core.errors import ProviderCancelled
from cyberpod_core.models import CreateSession, Principal, ResourceRef
from cyberpod_core.registry import LabRegistry
from cyberpod_core.repository import SQLiteRepository

ROOT = Path(__file__).resolve().parents[1]
ALICE = Principal(subject="student-a")
BOB = Principal(subject="student-b")
ADMIN = Principal(subject="operator", scopes={"admin"})


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        # Core contract tests own a minimal catalog; product labs are added by their suites.
        shutil.copytree(ROOT / "labs/hello-lab", self.path / "labs/hello-lab")
        self.registry = LabRegistry(self.path / "labs")
        self.registry.reload()
        self.runtime = MemoryRuntime()
        self.repo = SQLiteRepository(self.path / "core.sqlite3")
        self.now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        self.engine = Engine(self.registry, self.repo, self.runtime, clock=lambda: self.now,
                             startup_timeout=1, operation_timeout=0.3, integration_timeout=0.3)

    async def asyncTearDown(self):
        await self.engine.close()
        self.repo.close()
        self.temp.cleanup()

    def use_runtime(self, runtime):
        self.runtime = runtime
        self.engine.runtime = runtime

    async def create(self, owner=ALICE, lab="hello-lab", key=None):
        session, _ = await self.engine.create(lab, owner, CreateSession(), key)
        return session

    async def operate(self, session, action, owner=ALICE):
        await self.engine.command(str(session.session_id), action, owner)
        await self.engine.wait_idle(str(session.session_id))
        return self.repo.get(str(session.session_id))

    def edit_manifest(self, edit):
        path = self.path / "labs" / "hello-lab" / "lab.yaml"
        definition = yaml.safe_load(path.read_text())
        edit(definition)
        path.write_text(yaml.safe_dump(definition))
        self.registry.reload()


class PausingRuntime(MemoryRuntime):
    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def start(self, context, cancel, report):
        resource = ResourceRef(provider_id=str(uuid4()), kind="network",
                               logical_id=context.definition.networks[0].id, labels=context.labels)
        self.resources[resource.provider_id] = resource
        await report(resource)
        self.entered.set()
        await self.release.wait()
        if cancel.is_set():
            raise ProviderCancelled()
        return await super().start(context, cancel, report)


class ExplodingRuntime(MemoryRuntime):
    def __init__(self, report_resource=True):
        super().__init__()
        self.report_resource = report_resource

    async def start(self, context, cancel, report):
        resource = ResourceRef(provider_id=str(uuid4()), kind="network",
                               logical_id=context.definition.networks[0].id, labels=context.labels)
        self.resources[resource.provider_id] = resource
        if self.report_resource:
            await report(resource)
        raise RuntimeError("SECRET-EXCEPTION-CONTENT")


class FailingCleanupRuntime(MemoryRuntime):
    fail = True

    async def cleanup(self, context):
        if self.fail:
            owned = self._owned(context)
            if owned:
                self.resources.pop(owned[0].provider_id)
            raise RuntimeError("cleanup unavailable")
        await super().cleanup(context)
