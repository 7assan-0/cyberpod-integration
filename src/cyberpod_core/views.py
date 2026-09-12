"""Explicit public allowlists; internal manifest/configuration never leaks by default."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from .models import (
    Event, Failure, Model, Operation, Progress, Session, Status,
)


class TaskView(Model):
    id: str
    title: str
    instructions: str
    depends_on: list[str]


class FlagView(Model):
    id: str
    label: str


class LabView(Model):
    id: str
    name: str
    description: str
    version: str
    difficulty: str
    estimated_duration_minutes: int
    session_timeout_seconds: int
    required_tools: list[str]
    status: Literal["AVAILABLE", "DISABLED", "UNAVAILABLE"]
    unavailable_reason: str | None
    execution_mode: Literal["simulated", "infrastructure"]
    tasks: list[TaskView]
    flags: list[FlagView]
    browser_required: bool
    terminal_required: bool


class LabList(Model):
    labs: list[LabView]


class ResourceView(Model):
    kind: str
    logical_id: str


class SessionView(Model):
    session_id: UUID
    lab_id: str
    lab_version: str
    generation: int
    status: Status
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    server_time: datetime
    expired: bool
    revision: int
    resources: list[ResourceView]
    progress: Progress
    operation: Operation | None
    stop_requested: bool
    cleanup_requested: bool
    error: Failure | None
    execution_mode: Literal["simulated", "infrastructure"]


class SessionList(Model):
    sessions: list[SessionView]
    next_offset: int | None


class StatusView(Model):
    session_id: UUID
    status: Status
    generation: int
    revision: int
    expires_at: datetime
    server_time: datetime
    expired: bool
    completed: bool
    operation: Operation | None
    cleanup_requested: bool
    error: Failure | None


class EventList(Model):
    events: list[Event]
    next_cursor: int


class ErrorDetail(Model):
    code: str
    message: str
    request_id: UUID


class ErrorResponse(Model):
    error: ErrorDetail


class HealthView(Model):
    status: Literal["ok", "unavailable"]
    execution_mode: Literal["simulated", "infrastructure"]


class ReloadView(Model):
    registered: int


class Pagination(Model):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class SessionPagination(Model):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class EmptyRequest(Model):
    pass


def lab_view(lab, engine) -> LabView:
    status, reason = engine.availability(lab)
    return LabView(
        **lab.model_dump(include={"id", "name", "description", "version", "difficulty",
                                  "estimated_duration_minutes", "session_timeout_seconds", "required_tools"}),
        status=status, unavailable_reason=reason,
        execution_mode="simulated" if engine.runtime.simulated else "infrastructure",
        tasks=[TaskView(**t.model_dump(include=set(TaskView.model_fields))) for t in lab.tasks],
        flags=[FlagView(id=f.id, label=f.label) for f in lab.flags],
        browser_required=lab.browser.required, terminal_required=lab.terminal.required,
    )


def session_view(session: Session, engine) -> SessionView:
    return SessionView(
        **session.model_dump(include=set(SessionView.model_fields) - {"resources"}),
        resources=[ResourceView(kind=r.kind, logical_id=r.logical_id) for r in session.resources],
        server_time=engine.clock(), expired=engine.clock() >= session.expires_at,
        execution_mode="simulated" if engine.runtime.simulated else "infrastructure",
    )


def status_view(session, engine) -> StatusView:
    values = session_view(session, engine).model_dump(include=set(StatusView.model_fields))
    return StatusView(**values, completed=session.progress.completed)

