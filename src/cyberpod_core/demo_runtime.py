"""SIMULATED provider for Core acceptance tests and local API exploration only.

No containers, ports, processes, networks or desktops are created by this module.
Production must inject Astra #2's Runtime implementation, never this provider.
"""
import asyncio
from uuid import uuid4

from .errors import CoreError, ProviderCancelled
from .models import EndpointRef, ResourceRef, RuntimeSnapshot, SessionContext


class MemoryRuntime:
    name = "memory-demo-v1"
    simulated = True

    def __init__(self):
        self.resources: dict[str, ResourceRef] = {}
        self.health: dict[tuple[str, int], str] = {}

    def validate_lab(self, definition):
        if definition.extensions or any(x.extensions for x in [*definition.containers, *definition.networks,
                                                               *definition.volumes]):
            raise CoreError("UNSUPPORTED_EXTENSION", "Runtime does not support these extensions", 503)

    async def available(self):
        return True

    def _owned(self, context):
        return [r.model_copy(deep=True) for r in self.resources.values()
                if all(r.labels.get(k) == v for k, v in context.labels.items())]

    async def start(self, context: SessionContext, cancel: asyncio.Event, report):
        self.validate_lab(context.definition)
        groups = [("network", context.definition.networks), ("volume", context.definition.volumes),
                  ("container", context.definition.containers)]
        for kind, definitions in groups:
            for definition in definitions:
                if cancel.is_set():
                    raise ProviderCancelled()
                existing = next((r for r in self._owned(context)
                                 if r.kind == kind and r.logical_id == definition.id), None)
                resource = existing or ResourceRef(provider_id=str(uuid4()), kind=kind,
                                                    logical_id=definition.id, labels=context.labels)
                self.resources[resource.provider_id] = resource
                await report(resource)
                await asyncio.sleep(0)
        self.health[(str(context.session_id), context.generation)] = "READY"
        return await self.inspect(context)

    async def stop(self, context):
        self.health[(str(context.session_id), context.generation)] = "STOPPED"

    async def cleanup(self, context):
        for resource in self._owned(context):
            self.resources.pop(resource.provider_id, None)
        self.health.pop((str(context.session_id), context.generation), None)

    async def inspect(self, context):
        resources = self._owned(context)
        health = self.health.get((str(context.session_id), context.generation),
                                 "STARTING" if resources else "STOPPED")
        endpoints = []
        if health == "READY":
            for kind in ("browser", "terminal"):
                if getattr(context.definition, kind).required:
                    endpoints.append(EndpointRef(kind=kind, service_ref=f"simulated:{context.namespace}:{kind}"))
        return RuntimeSnapshot(health=health, resources=resources, endpoints=endpoints)

