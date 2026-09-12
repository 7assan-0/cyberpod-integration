"""OpenAPI 3.1 generated from the exact request and public response models."""
import json
from pathlib import Path

from .models import (
    AccessGrant, CreateSession, Evaluation, FlagSubmission, LabDefinition, SessionContext,
)
from .views import (
    EmptyRequest, ErrorResponse, EventList, HealthView, LabList, LabView,
    ReloadView, SessionList, SessionView, StatusView,
)


def document():
    schemas = {}
    for model in (AccessGrant, CreateSession, Evaluation, FlagSubmission, SessionContext,
                  EmptyRequest, ErrorResponse, EventList, HealthView, LabList, LabView,
                  ReloadView, SessionList, SessionView, StatusView):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema

    def response(name):
        return {"description": name, "content": {"application/json": {
            "schema": {"$ref": f"#/components/schemas/{name}"}}}}

    paths = {}

    def add(path, method, output, summary, *, body=None, codes=(200,), public=False, scope="student", query=()):
        parameters = []
        for name in ("lab_id", "session_id"):
            if "{" + name + "}" in path:
                parameters.append({"name": name, "in": "path", "required": True,
                                   "schema": {"type": "string", **({"format": "uuid"} if name == "session_id" else {})}})
        for name, maximum in query:
            parameters.append({"name": name, "in": "query", "schema": {
                "type": "integer", "minimum": 1 if name == "limit" else 0,
                **({"maximum": maximum} if maximum else {})}})
        operation = {"summary": summary, "operationId": method + path.replace("/", "_").replace("{", "").replace("}", ""),
                     "security": [] if public else [{"BearerAuth": []}],
                     "x-required-scope": None if public else scope,
                     "parameters": parameters,
                     "responses": {**{str(code): response(output) for code in codes},
                                   **{str(code): response("ErrorResponse") for code in (401,403,404,409,410,413,415,422,429,500,503)}}}
        if body:
            operation["requestBody"] = {"required": body not in {"EmptyRequest", "CreateSession"},
                                         "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body}"}}}}
        if method == "post" and path == "/labs/{lab_id}/sessions":
            parameters.append({"name": "Idempotency-Key", "in": "header", "schema": {
                "type": "string", "minLength": 1, "maxLength": 128},
                "description": "Opaque printable ASCII key, scoped to authenticated owner; conflicting body returns 409."})
        paths.setdefault(path, {})[method] = operation

    add("/healthz", "get", "HealthView", "Liveness and explicit execution mode", public=True)
    add("/readyz", "get", "HealthView", "Registry/runtime readiness", public=True)
    paths["/readyz"]["get"]["responses"]["503"] = response("HealthView")
    add("/labs", "get", "LabList", "Discover lab metadata")
    add("/labs/{lab_id}", "get", "LabView", "Lab details and availability")
    add("/labs/{lab_id}/sessions", "post", "SessionView", "Create an owned session; does not start it",
        body="CreateSession", codes=(201, 200))
    add("/sessions", "get", "SessionList", "List only the current user's sessions", query=(("offset", None), ("limit", 200)))
    add("/sessions/{session_id}", "get", "SessionView", "Read owned session")
    add("/sessions/{session_id}/status", "get", "StatusView", "Poll authoritative lifecycle status")
    for action in ("start", "stop", "restart", "cleanup"):
        add(f"/sessions/{{session_id}}/{action}", "post", "SessionView", f"Request {action}; poll status until settled",
            body="EmptyRequest", codes=(202, 200))
    add("/sessions/{session_id}/events", "get", "EventList", "Read persisted session events", query=(("after", None), ("limit", 200)))
    add("/sessions/{session_id}/flags", "post", "SessionView", "Forward a submission to the registered Validator",
        body="FlagSubmission")
    add("/sessions/{session_id}/access", "get", "AccessGrant", "Issue short-lived session access")
    add("/internal/events", "get", "EventList", "Poll global durable event feed", scope="validator", query=(("after", None), ("limit", 200)))
    add("/internal/sessions/{session_id}/context", "get", "SessionContext", "Trusted integration context, including private configuration", scope="validator")
    add("/internal/sessions/{session_id}/evaluation", "put", "SessionView", "Persist an authoritative absolute evaluation snapshot",
        body="Evaluation", scope="validator")
    add("/admin/labs/reload", "post", "ReloadView", "Atomically rediscover and register manifests", body="EmptyRequest", scope="admin")
    for path, summary in (("/openapi.json", "This API contract"), ("/schemas/lab.schema.json", "Lab manifest JSON Schema")):
        paths[path] = {"get": {"summary": summary, "security": [], "responses": {
            "200": {"description": "JSON document", "content": {"application/json": {"schema": {"type": "object"}}}}}}}
    return {"openapi": "3.1.0", "info": {"title": "CyberPod Core API", "version": "1.0.0",
            "description": "Core contract v1.0. Lifecycle and progress are independent. Infrastructure/Validator adapters are required outside demo mode."},
            "servers": [{"url": "/"}], "paths": paths,
            "components": {"schemas": schemas, "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer"}}}}


def export(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "openapi.json").write_text(json.dumps(document(), indent=2) + "\n")
    lab_schema = LabDefinition.model_json_schema()
    lab_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    (directory / "lab.schema.json").write_text(json.dumps(lab_schema, indent=2) + "\n")


if __name__ == "__main__":
    export(Path("schemas"))

