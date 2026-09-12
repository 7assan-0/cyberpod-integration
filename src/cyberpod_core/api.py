import asyncio
import json
import logging
from contextlib import suppress
from uuid import UUID, uuid4

from aiohttp import web
from pydantic import BaseModel, ValidationError

from .engine import Engine
from .errors import CoreError
from .models import CreateSession, Evaluation, FlagSubmission, LabDefinition, context_for
from .ports import IdentityProvider
from .views import (
    EmptyRequest, EventList, HealthView, LabList, Pagination, ReloadView,
    SessionList, SessionPagination, lab_view, session_view, status_view,
)

ENGINE = web.AppKey("engine", Engine)
AUTH = web.AppKey("identity_provider", object)
log = logging.getLogger("cyberpod.http")
PUBLIC = {"/healthz", "/readyz", "/openapi.json", "/schemas/lab.schema.json"}


def respond(value, status=200, headers=None):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return web.json_response(value, status=status, headers=headers)


def reject_constant(_):
    raise ValueError("JSON constants must be finite")


async def body(request, model):
    data = await request.read()
    if not data:
        return model.model_validate({})
    if request.content_type != "application/json":
        raise CoreError("CONTENT_TYPE", "Use application/json", 415)
    try:
        value = json.loads(data, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CoreError("INVALID_JSON", "Request body must be valid JSON", 422) from exc
    return model.model_validate(value)


def session_id(request):
    try:
        return str(UUID(request.match_info["session_id"]))
    except ValueError as exc:
        raise CoreError("INVALID_SESSION_ID", "Session ID must be a UUID", 422) from exc


def require(request, scope):
    principal = request["principal"]
    if scope not in principal.scopes and "admin" not in principal.scopes:
        raise CoreError("FORBIDDEN", f"{scope.capitalize()} scope required", 403)
    return principal


@web.middleware
async def boundary(request, handler):
    request_id = str(uuid4())
    try:
        if request.path not in PUBLIC:
            authorization = request.headers.get("Authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token or len(token) > 8192:
                raise CoreError("UNAUTHORIZED", "Valid bearer authentication required", 401)
            principal = await request.app[AUTH].authenticate(token)
            if principal is None:
                raise CoreError("UNAUTHORIZED", "Valid bearer authentication required", 401)
            request["principal"] = principal
        response = await handler(request)
    except CoreError as exc:
        response = respond({"error": {"code": exc.code, "message": exc.message, "request_id": request_id}}, exc.status)
    except ValidationError:
        # Validation errors can contain submitted secrets; do not serialize .errors().
        response = respond({"error": {"code": "VALIDATION_ERROR", "message": "Request does not match the API schema",
                                       "request_id": request_id}}, 422)
    except web.HTTPException as exc:
        response = respond({"error": {"code": f"HTTP_{exc.status}", "message": exc.reason,
                                       "request_id": request_id}}, exc.status)
    except Exception as exc:
        log.error("request_failed", extra={"request_id": request_id, "error_type": type(exc).__name__})
        response = respond({"error": {"code": "INTERNAL_ERROR", "message": "Request could not be completed",
                                       "request_id": request_id}}, 500)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if response.status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    route = request.match_info.route.resource
    log.info("http_request", extra={"request_id": request_id, "method": request.method,
                                    "route": route.canonical if route else "unmatched", "http_status": response.status})
    return response


async def health(request):
    engine = request.app[ENGINE]
    healthy = True
    if request.path == "/readyz":
        try:
            healthy = bool(engine.registry.all()) and await engine._call(engine.runtime.available(), 3)
        except Exception:
            healthy = False
    return respond(HealthView(status="ok" if healthy else "unavailable",
                              execution_mode="simulated" if engine.runtime.simulated else "infrastructure"),
                   200 if healthy else 503)


async def list_labs(request):
    require(request, "student")
    engine = request.app[ENGINE]
    return respond(LabList(labs=[lab_view(lab, engine) for lab in engine.registry.all()]))


async def get_lab(request):
    require(request, "student")
    engine = request.app[ENGINE]
    return respond(lab_view(engine.registry.get(request.match_info["lab_id"]), engine))


async def create_session(request):
    principal = require(request, "student")
    engine = request.app[ENGINE]
    session, created = await engine.create(request.match_info["lab_id"], principal,
                                           await body(request, CreateSession), request.headers.get("Idempotency-Key"))
    return respond(session_view(session, engine), 201 if created else 200,
                   {"Location": f"/sessions/{session.session_id}"})


async def get_session(request):
    principal = require(request, "student")
    engine = request.app[ENGINE]
    session = engine.get(session_id(request), principal)
    return respond(status_view(session, engine) if request.path.endswith("/status") else session_view(session, engine))


async def list_sessions(request):
    principal = require(request, "student")
    engine = request.app[ENGINE]
    page = SessionPagination.model_validate(dict(request.query))
    sessions = engine.repo.all(principal.subject)
    values = sessions[page.offset:page.offset + page.limit]
    end = page.offset + len(values)
    return respond(SessionList(sessions=[session_view(s, engine) for s in values],
                               next_offset=end if end < len(sessions) else None))


async def lifecycle(request):
    principal = require(request, "student")
    engine = request.app[ENGINE]
    await body(request, EmptyRequest)
    session = await engine.command(session_id(request), request.match_info["action"], principal)
    return respond(session_view(session, engine), 202 if session.operation else 200,
                   {"Location": f"/sessions/{session.session_id}/status"})


async def events(request):
    engine = request.app[ENGINE]
    page = Pagination.model_validate(dict(request.query))
    sid = None
    if request.path.startswith("/internal/"):
        require(request, "validator")
    else:
        principal = require(request, "student")
        sid = session_id(request)
        engine.get(sid, principal)
    values = engine.repo.events(page.after, page.limit, sid)
    return respond(EventList(events=values, next_cursor=values[-1].sequence if values else page.after))


async def submit_flag(request):
    principal = require(request, "student")
    engine = request.app[ENGINE]
    submission = await body(request, FlagSubmission)
    session = await engine.submit_flag(session_id(request), principal, submission)
    return respond(session_view(session, engine))


async def access(request):
    principal = require(request, "student")
    return respond(await request.app[ENGINE].access(session_id(request), principal))


async def evaluation(request):
    require(request, "validator")
    engine = request.app[ENGINE]
    session = await engine.apply_evaluation(session_id(request), await body(request, Evaluation))
    return respond(session_view(session, engine))


async def context(request):
    require(request, "validator")
    return respond(context_for(request.app[ENGINE].repo.get(session_id(request))))


async def reload_labs(request):
    require(request, "admin")
    await body(request, EmptyRequest)
    return respond(ReloadView(registered=request.app[ENGINE].registry.reload()))


async def schema(request):
    return respond(LabDefinition.model_json_schema())


async def openapi(request):
    from .openapi import document
    return respond(document())


def create_app(engine: Engine, identity_provider: IdentityProvider, *, sweep_interval=10.0,
               recover=True, own_repository=True) -> web.Application:
    app = web.Application(middlewares=[boundary], client_max_size=16384)
    app[ENGINE], app[AUTH] = engine, identity_provider
    app.add_routes([
        web.get("/healthz", health), web.get("/readyz", health),
        web.get("/openapi.json", openapi), web.get("/schemas/lab.schema.json", schema),
        web.get("/labs", list_labs), web.get("/labs/{lab_id}", get_lab),
        web.post("/labs/{lab_id}/sessions", create_session),
        web.get("/sessions", list_sessions), web.get("/sessions/{session_id}", get_session),
        web.get("/sessions/{session_id}/status", get_session),
        web.get("/sessions/{session_id}/events", events),
        web.get("/sessions/{session_id}/access", access),
        web.post("/sessions/{session_id}/flags", submit_flag),
        web.post("/sessions/{session_id}/{action:start|stop|restart|cleanup}", lifecycle),
        web.get("/internal/events", events),
        web.get("/internal/sessions/{session_id}/context", context),
        web.put("/internal/sessions/{session_id}/evaluation", evaluation),
        web.post("/admin/labs/reload", reload_labs),
    ])

    async def monitor():
        while True:
            await asyncio.sleep(sweep_interval)
            try:
                await engine.sweep_once()
            except Exception as exc:
                log.error("monitor_failed", extra={"error_type": type(exc).__name__})

    async def lifetime(_):
        task = None
        try:
            if recover:
                await engine.reconcile()
            if sweep_interval > 0:
                task = asyncio.create_task(monitor(), name="session-monitor")
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            try:
                await engine.close()
            finally:
                if own_repository:
                    engine.repo.close()

    app.cleanup_ctx.append(lifetime)
    return app
