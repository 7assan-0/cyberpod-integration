# CyberPod Core HTTP API — contract 1.0

The full request/response schemas are in `schemas/openapi.json`, also served at `/openapi.json`. Paths below are relative to the Core API origin. The example development origin is `http://127.0.0.1:8000`; no frontend is served at `/`.

## Authentication

Send `Authorization: Bearer <token>`. The authenticated principal supplies ownership; user IDs from JSON bodies, query strings and `X-User-ID` are never accepted as identity. Unauthorized requests return 401. A student requesting another student's session receives 404. Admin can inspect/manage specific sessions across owners. `/sessions` lists only the authenticated subject's own sessions, including for admin.

The bootstrap `StaticTokens` implementation reads a private JSON array of `{token, subject, scopes}` records. Scopes are `student`, `validator` and `admin`; admin includes the other permissions. Use independently generated tokens of at least 24 characters. Keep credential files outside the source tree. There is no token issuance/login route; a production `IdentityProvider.authenticate` adapter is the login integration boundary.

Only `/healthz`, `/readyz`, `/openapi.json` and `/schemas/lab.schema.json` are public. Internal routes expose private configuration and require the trusted Validator service role or admin. They must not be reachable using a student token.

## Endpoints

| Method and path | Scope | Result |
| --- | --- | --- |
| `GET /healthz` | Public | 200 liveness and execution mode |
| `GET /readyz` | Public | 200 or 503 registry/runtime readiness |
| `GET /openapi.json` | Public | Complete OpenAPI 3.1 contract |
| `GET /schemas/lab.schema.json` | Public | Manifest JSON Schema |
| `GET /labs` | Student | `{labs: LabView[]}`; includes availability |
| `GET /labs/{lab_id}` | Student | LabView, including public tasks and flag labels |
| `POST /labs/{lab_id}/sessions` | Student | 201 new CREATED session, or 200 idempotent replay |
| `GET /sessions?offset=0&limit=100` | Student | Own sessions and nullable `next_offset` |
| `GET /sessions/{session_id}` | Owner/admin | Full public SessionView |
| `GET /sessions/{session_id}/status` | Owner/admin | Compact state, generation, operation, TTL and error |
| `POST /sessions/{session_id}/start` | Owner/admin | 202 operation accepted or 200 stable no-op |
| `POST /sessions/{session_id}/stop` | Owner/admin | 202 operation accepted or 200 stable no-op |
| `POST /sessions/{session_id}/restart` | Owner/admin | 202 attempt reset accepted |
| `POST /sessions/{session_id}/cleanup` | Owner/admin | 202 destruction accepted or 200 already cleaned |
| `GET /sessions/{session_id}/events?after=0&limit=100` | Owner/admin | Durable event page and `next_cursor` |
| `GET /sessions/{session_id}/access` | Owner/admin | Short-lived AccessGrant from the access adapter |
| `POST /sessions/{session_id}/flags` | Owner/admin | Validator call and authoritative updated SessionView |
| `GET /internal/events?after=0&limit=100` | Validator/admin | Global durable Core event feed |
| `GET /internal/sessions/{session_id}/context` | Validator/admin | Full pinned integration context; private |
| `PUT /internal/sessions/{session_id}/evaluation` | Validator/admin | Apply an absolute evaluator snapshot |
| `POST /admin/labs/reload` | Admin | `{registered: number}` after atomic manifest reload |

List/event limits are 1–200. `after` is an exclusive sequence cursor. Unknown request fields and unknown pagination parameters are rejected. IDs for sessions must parse as UUIDs; invalid IDs return 422, absent sessions 404.

## Create and lifecycle requests

Create accepts an optional JSON body `{}` or `{"ttl_seconds": 600}`. TTL must be at least 60 seconds and no greater than the manifest limit. No owner, resource specification, score, Lab configuration override or target address can be supplied by students.

An optional `Idempotency-Key` header uses 1–128 printable ASCII characters and is scoped to the owner. The same key and create request returns the original session even after it has been cleaned. A changed request with that key returns 409. Use a new key for a new session. Create responses include `Location: /sessions/{uuid}`.

Start/stop/restart/cleanup accept no body or `{}`. Operation responses include `Location: /sessions/{uuid}/status`. A 202 response contains an operation UUID, action and start timestamp. Poll the status endpoint until `operation` is null and inspect status/error. An error can finish an operation without reaching the requested state.

Stop/cleanup can be queued during startup. `stop_requested`/`cleanup_requested` show durable intent; the displayed operation can still be the original start/reset while its worker finishes cancellation. A repeated in-progress action returns the same operation. Start at RUNNING, stop at STOPPED/CLEANED, and cleanup at CLEANED are no-ops. Restart is a new reset when repeated **after** a completed restart; clients must not blindly retry completed restart requests.

The default limits are three non-cleaned sessions per owner and 100 across the control process. STOPPED/ERROR records still count because their resources can remain allocated. Expired sessions count until cleanup succeeds. These limits are configurable in Engine; real host admission remains the Runtime's responsibility.

## Session and status responses

`SessionView` contains `session_id`, `lab_id`, `lab_version`, `generation`, `status`, UTC timestamps, server time, `expired`, optimistic `revision`, safe resource kind/logical-ID summaries, progress, operation, intent flags, error and execution mode. The session model is deliberately distinct from the private stored Session.

`progress` contains `score`, `maximum_score`, `completed`, `completed_tasks`, flag statuses and evaluator `revision`. These are the last stored Validator result. `status: RUNNING` and `progress.completed: true` can coexist. The compact status endpoint includes `completed` directly for polling but not the full score snapshot; refresh GET session after an evaluation event.

The backend is authoritative for expiry. The UI can derive a countdown from `expires_at - server_time`. `expired: true` may temporarily coexist with an older lifecycle state while cleanup is queued; access, new start/reset and evaluation are already denied. API clients must not extend expiry locally.

## Flag submission

```json
{
  "submission_id": "ed337e71-4c1a-4d80-ab3c-14275463e1a0",
  "generation": 1,
  "flag_id": "completion",
  "value": "student-supplied-value"
}
```

This is a contract example for a Lab declaring that flag; `hello-lab` does not require a flag or Validator. The payload is forwarded only after owner, active generation, RUNNING state, known flag ID and TTL checks. It is not put in logs/events/persistent Core state. Astra #6 verifies the actual value, per-session secret, evidence, prerequisites and scoring.

Validator results are rechecked against current lifecycle and generation after the call, so a response arriving after stop/reset cannot score the previous attempt. The Validator owns idempotency for `(session_id, generation, submission_id)` and must reject changed contents under an existing submission ID. A Validator failure returns 503 and leaves the prior progress unchanged. A wrong flag can return a normal 200 SessionView with a rejected flag status, as supplied by the Validator.

## Trusted evaluation callback

```json
{
  "result_id": "38b32a90-1a52-4e65-9665-fda4df18b9f9",
  "session_id": "878ecbfb-f840-4598-b468-c27cd197bb10",
  "lab_id": "your-lab",
  "generation": 1,
  "revision": 1,
  "score": 37,
  "maximum_score": 100,
  "completed": false,
  "completed_tasks": [],
  "flags": {}
}
```

The trusted result is an absolute snapshot, never a score increment. A duplicate result ID with the same content is a no-op. Different content under that ID or a stale revision is rejected. Session ID, Lab, generation, active lifecycle, TTL and known task/flag references must match. A result for another session is rejected even if Lab and generation are identical. Core performs generic envelope checks such as finite nonnegative score bounded by supplied maximum; it does not decide educational points, evidence order, accepted flag content or completion requirements.

## Access grants

GET access returns `session_id`, `generation`, `expires_at`, and optional `browser_url`/`terminal_url`. URLs must be HTTPS or relative to the same origin. Required interfaces must have a URL. Grants expire within five minutes and no later than the session deadline. The response is not cached. A stopped, expired, wrong-owner or stale-generation session cannot obtain a grant.

Astra #2/#3 must implement actual signed tickets, gateway identity checks, immediate revocation, expiry and WebSocket enforcement. Core itself does not serve a desktop or proxy WebSockets.

## Error and logging contract

All API error responses use this shape:

```json
{
  "error": {
    "code": "SESSION_EXPIRED",
    "message": "Session has expired",
    "request_id": "edb4c0e2-e98c-4920-9bdf-9e252eb1c76f"
  }
}
```

`X-Request-ID` is generated by the server and accompanies successful responses too. Request bodies are limited to 16 KiB; submitted flag values to 4096 characters. Responses include `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

| HTTP | Common codes |
| --- | --- |
| 401 | `UNAUTHORIZED` |
| 403 | `FORBIDDEN` |
| 404 | `LAB_NOT_FOUND`, `SESSION_NOT_FOUND` |
| 409 | `INVALID_STATE`, `SESSION_CLEANED`, `OPERATION_IN_PROGRESS`, `IDEMPOTENCY_CONFLICT`, `EVALUATION_SCOPE`, `STALE_EVALUATION`, `EVALUATION_CONFLICT` |
| 410 | `SESSION_EXPIRED` |
| 413 | `HTTP_413` |
| 415 | `CONTENT_TYPE` |
| 422 | `VALIDATION_ERROR`, `INVALID_JSON`, `INVALID_SESSION_ID`, `INVALID_IDEMPOTENCY_KEY`, `TTL_EXCEEDS_LIMIT`, `UNKNOWN_TASK`, `UNKNOWN_FLAG` |
| 429 | `SESSION_QUOTA` |
| 503 | `VALIDATOR_UNAVAILABLE`, `VALIDATOR_FAILED`, `ACCESS_UNAVAILABLE`, `ACCESS_FAILED`, `RUNTIME_MISMATCH`, unavailable Lab dependencies |

Asynchronous provider failure is returned later in SessionView with `status: ERROR`, `error.code` (for example `RUNTIME_TIMEOUT` or `RUNTIME_OPERATION_FAILED`) and a retryable message. It is not a retroactive HTTP error for the original 202 request. `/readyz` is the exception to the error envelope: its 503 response is a `HealthView`.
