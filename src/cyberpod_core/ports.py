"""Version 1.0 integration contracts; adapters are registered by the application."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from .models import (
    AccessGrant, Evaluation, FlagSubmission, LabDefinition, Principal,
    ResourceRef, RuntimeSnapshot, SessionContext,
)

ResourceReporter = Callable[[ResourceRef], Awaitable[None]]


class Runtime(Protocol):
    name: str
    simulated: bool

    def validate_lab(self, definition: LabDefinition) -> None:
        """Fail closed on unsupported limits, network policies or extensions."""
        ...

    async def available(self) -> bool: ...

    async def start(self, context: SessionContext, cancel: asyncio.Event,
                    report: ResourceReporter) -> RuntimeSnapshot:
        """Idempotent ensure-running. Label at allocation; report every resource.

        Do not return READY until all healthchecks and required endpoints pass.
        Poll cancel; raise ProviderCancelled without losing discoverable resources.
        Honor coroutine cancellation/timeouts. Never leave untracked background work.
        """
        ...

    async def stop(self, context: SessionContext) -> None:
        """Idempotently halt workloads and revoke ingress; preserve reusable resources."""
        ...

    async def cleanup(self, context: SessionContext) -> None:
        """Discover by ALL ownership labels, revoke ingress, remove everything owned.

        Include allocations absent from context.resources; never prune globally.
        A missing resource is success. Retain uncertain ownership for investigation.
        """
        ...

    async def inspect(self, context: SessionContext) -> RuntimeSnapshot:
        """Authoritative label-scoped inventory, including partially created resources."""
        ...


class Validator(Protocol):
    async def submit(self, context: SessionContext, submission: FlagSubmission) -> Evaluation:
        """Astra #6 owns evidence, flags, rules, scores, ordering and replay protection.

        Idempotency key = (session_id, generation, submission_id). Return an absolute,
        monotonic result snapshot. Bind evidence to this session and generation.
        """
        ...


class AccessBroker(Protocol):
    async def issue(self, context: SessionContext, principal: Principal) -> AccessGrant:
        """Issue short-lived session+generation-bound access after gateway authorization.

        Astra #3/#2 enforce revocation and authorization on every HTTP/WS connection.
        Do not return raw backend service references or permanent VNC credentials.
        """
        ...


class IdentityProvider(Protocol):
    async def authenticate(self, bearer_token: str) -> Principal | None: ...

