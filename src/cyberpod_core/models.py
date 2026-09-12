from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
Text = Annotated[str, Field(min_length=1, max_length=8192)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Extensible(Model):
    extensions: dict[str, JsonValue] = Field(default_factory=dict)


class ResourceLimits(Model):
    cpu: float = Field(default=1, gt=0, le=64)
    memory_mb: int = Field(default=512, ge=32, le=262144)
    pids: int = Field(default=128, ge=8, le=65536)
    storage_mb: int = Field(default=1024, ge=16, le=1048576)


class Network(Extensible):
    id: Slug
    scope: Literal["session"] = "session"
    internet: Literal["none", "restricted", "allowed"] = "none"
    egress_allowlist: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def restricted_egress(self):
        if (self.internet == "restricted") != bool(self.egress_allowlist):
            raise ValueError("Only restricted networks must specify an egress allowlist")
        return self


class Volume(Extensible):
    id: Slug
    ephemeral: Literal[True] = True
    size_mb: int = Field(default=256, ge=16, le=1048576)


class Mount(Model):
    volume: Slug
    path: str = Field(pattern=r"^/[^\x00]*$", max_length=512)
    read_only: bool = False


class Healthcheck(Model):
    command: list[str] = Field(min_length=1, max_length=64)
    interval_seconds: int = Field(default=2, ge=1, le=300)
    timeout_seconds: int = Field(default=2, ge=1, le=60)
    retries: int = Field(default=15, ge=1, le=120)


class Container(Extensible):
    id: Slug
    image: str = Field(min_length=1, max_length=512)
    role: str = Field(default="target", min_length=1, max_length=64)
    command: list[str] = Field(default_factory=list, max_length=128)
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    networks: list[Slug] = Field(min_length=1)
    ports: list[Annotated[int, Field(ge=1, le=65535)]] = Field(default_factory=list)
    mounts: list[Mount] = Field(default_factory=list)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    healthcheck: Healthcheck
    read_only_rootfs: bool = True
    run_as: str = Field(default="1000:1000", pattern=r"^[0-9]+:[0-9]+$")


class TaskDefinition(Extensible):
    id: Slug
    title: Text
    instructions: str = Field(default="", max_length=16384)
    depends_on: list[Slug] = Field(default_factory=list)


class Plugin(Extensible):
    id: Slug
    config: dict[str, JsonValue] = Field(default_factory=dict)


class FlagDefinition(Extensible):
    id: Slug
    label: Text
    secret_ref: str | None = Field(default=None, max_length=512)


class AccessRequirement(Extensible):
    required: bool = False
    container: Slug | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Literal["http", "websocket"] = "http"
    path: str = Field(default="/", pattern=r"^/[^\x00]*$", max_length=512)

    @model_validator(mode="after")
    def required_endpoint(self):
        if self.required and (self.container is None or self.port is None):
            raise ValueError("Required access needs a container and port")
        return self


class LabDefinition(Extensible):
    schema_version: Literal["1.0"] = "1.0"
    id: Slug
    name: Text
    description: Text
    version: str = Field(default="1.0.0", min_length=1, max_length=64)
    enabled: bool = True
    difficulty: Literal["beginner", "intermediate", "advanced", "expert"]
    estimated_duration_minutes: int = Field(ge=1, le=1440)
    session_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    required_tools: list[str] = Field(default_factory=list, max_length=100)
    containers: list[Container] = Field(min_length=1, max_length=32)
    networks: list[Network] = Field(min_length=1, max_length=16)
    volumes: list[Volume] = Field(default_factory=list, max_length=32)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)
    tasks: list[TaskDefinition] = Field(default_factory=list, max_length=200)
    validator: Plugin | None = None
    scoring: Plugin | None = None
    flags: list[FlagDefinition] = Field(default_factory=list, max_length=100)
    browser: AccessRequirement = Field(default_factory=AccessRequirement)
    terminal: AccessRequirement = Field(default_factory=AccessRequirement)

    @model_validator(mode="after")
    def references(self):
        for group in (self.containers, self.networks, self.volumes, self.tasks, self.flags):
            ids = [v.id for v in group]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate logical IDs")
        networks, volumes = {x.id for x in self.networks}, {x.id for x in self.volumes}
        containers = {x.id: x for x in self.containers}
        tasks = {x.id: x for x in self.tasks}
        for container in self.containers:
            if not set(container.networks) <= networks:
                raise ValueError("Unknown container network")
            if not {m.volume for m in container.mounts} <= volumes:
                raise ValueError("Unknown volume")
        for dimension in ("cpu", "memory_mb", "pids", "storage_mb"):
            requested = sum(getattr(c.resources, dimension) for c in self.containers)
            if dimension == "storage_mb":
                requested += sum(v.size_mb for v in self.volumes)
            if requested > getattr(self.resources, dimension) + 1e-9:
                raise ValueError("Container and volume limits exceed the session budget")
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id not in tasks or task_id in visiting:
                raise ValueError("Unknown or cyclic task dependency")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in tasks[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
        for requirement in (self.browser, self.terminal):
            if requirement.container is not None:
                if requirement.container not in containers:
                    raise ValueError("Unknown access container")
                if requirement.port not in containers[requirement.container].ports:
                    raise ValueError("Access port must be declared on its container")
        if (self.flags or self.scoring) and self.validator is None:
            raise ValueError("Flags and scoring require a validator plugin")
        return self


class Status(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    RESETTING = "RESETTING"
    CLEANING = "CLEANING"
    CLEANED = "CLEANED"
    ERROR = "ERROR"


class ResourceRef(Model):
    provider_id: Text
    kind: str = Field(min_length=1, max_length=64)
    logical_id: Slug
    labels: dict[str, str]
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class EndpointRef(Model):
    kind: Literal["browser", "terminal"]
    service_ref: Text


class RuntimeSnapshot(Model):
    health: Literal["READY", "STARTING", "STOPPED", "FAILED", "UNKNOWN"]
    resources: list[ResourceRef] = Field(default_factory=list)
    endpoints: list[EndpointRef] = Field(default_factory=list)


class Progress(Model):
    score: float = Field(default=0, ge=0)
    maximum_score: float = Field(default=0, ge=0)
    completed: bool = False
    completed_tasks: list[Slug] = Field(default_factory=list)
    flags: dict[str, Literal["pending", "accepted", "rejected"]] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def envelope(self):
        if self.score > self.maximum_score:
            raise ValueError("Score exceeds maximum")
        if len(set(self.completed_tasks)) != len(self.completed_tasks):
            raise ValueError("Duplicate completed tasks")
        return self


class Evaluation(Progress):
    result_id: UUID
    session_id: UUID
    lab_id: Slug
    generation: int = Field(ge=1)
    revision: int = Field(ge=1)


class Failure(Model):
    code: str
    message: str
    retryable: bool = True


class Operation(Model):
    operation_id: UUID
    action: Literal["start", "stop", "restart", "cleanup"]
    started_at: AwareDatetime


class Session(Model):
    session_id: UUID
    owner_id: Text
    lab_id: Slug
    lab_version: str
    definition_hash: str
    definition: LabDefinition
    runtime_name: str
    deployment_id: UUID
    generation: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    expires_at: AwareDatetime
    status: Status = Status.CREATED
    revision: int = 0
    resources: list[ResourceRef] = Field(default_factory=list)
    endpoints: list[EndpointRef] = Field(default_factory=list)
    progress: Progress = Field(default_factory=Progress)
    operation: Operation | None = None
    stop_requested: bool = False
    cleanup_requested: bool = False
    error: Failure | None = None


class SessionContext(Model):
    session_id: UUID
    lab_id: Slug
    generation: int
    namespace: str
    labels: dict[str, str]
    definition_hash: str
    definition: LabDefinition
    expires_at: AwareDatetime
    status: Status
    revision: int
    progress: Progress
    resources: list[ResourceRef]
    endpoints: list[EndpointRef]


class CreateSession(Model):
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class FlagSubmission(Model):
    submission_id: UUID
    generation: int = Field(ge=1)
    flag_id: Slug
    value: str = Field(min_length=1, max_length=4096)


class AccessGrant(Model):
    session_id: UUID
    generation: int
    expires_at: AwareDatetime
    browser_url: str | None = None
    terminal_url: str | None = None


class Principal(Model):
    subject: Text
    scopes: set[Literal["student", "validator", "admin"]] = Field(default_factory=lambda: {"student"})


class Event(Model):
    sequence: int = 0
    event_id: UUID
    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    lab_id: Slug
    generation: int
    type: str
    source: Literal["core", "validator"] = "core"
    occurred_at: AwareDatetime
    data: dict[str, JsonValue] = Field(default_factory=dict)


def context_for(session: Session) -> SessionContext:
    return SessionContext(
        session_id=session.session_id, lab_id=session.lab_id,
        generation=session.generation,
        namespace=f"cp-{session.session_id.hex}-g{session.generation}",
        labels={
            "cyberpod.managed": "true",
            "cyberpod.deployment": str(session.deployment_id),
            "cyberpod.session": str(session.session_id),
            "cyberpod.generation": str(session.generation),
        },
        definition_hash=session.definition_hash,
        definition=session.definition.model_copy(deep=True),
        expires_at=session.expires_at,
        status=session.status, revision=session.revision,
        progress=session.progress.model_copy(deep=True),
        resources=[r.model_copy(deep=True) for r in session.resources],
        endpoints=[e.model_copy(deep=True) for e in session.endpoints],
    )
