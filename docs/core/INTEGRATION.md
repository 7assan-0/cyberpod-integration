# Integration handoff — Astra #1 → team

Contract version: **1.0**, implementation version **0.1.0**. No other Astra's source was available for a compatibility test. These are concrete, typed integration interfaces, not a claim that separately built components already conform to them. Review this contract with each component owner before combining packages; record requested changes instead of rewriting another owner's code.

## Astra #2 — infrastructure Runtime

Implement `cyberpod_core.ports.Runtime` and supply a trusted factory to `--runtime-factory module:callable`, or inject the instance directly into `Engine`.

| Member | Required behavior |
| --- | --- |
| `name: str` | Stable adapter identity, pinned in sessions; do not change across compatible upgrades |
| `simulated: bool` | False for the actual infrastructure implementation |
| `validate_lab(definition)` | Reject unsupported images/policy semantics/resource limits/extensions; never silently ignore requested isolation |
| `available()` | Asynchronously report control-plane availability |
| `start(context, cancel, report)` | Ensure all resources exist and workloads become ready; await each allocation report; honor cancellation |
| `stop(context)` | Halt owned workloads, revoke desktop/terminal ingress, preserve resumable volumes/networks |
| `cleanup(context)` | Revoke ingress and destroy all exactly label-owned resources, including unreported partial allocations |
| `inspect(context)` | Authoritative owned inventory, health and private endpoint references; empty ownership scope returns STOPPED |

`SessionContext` includes session/Lab IDs, generation, unique namespace, all ownership labels, pinned definition and hash, expiration, lifecycle/revision, prior progress, recorded resources and private endpoint references. It never asks infrastructure to choose a Lab by special-case code.

`ResourceRef` contains provider ID, kind, logical ID, ownership labels and private JSON metadata. Kinds `container`, `network` and `volume` must cover the manifest's logical resources before READY; additional kinds are permitted for temporary resources, access routes or secrets. `(kind, provider_id)` and `(kind, logical_id)` must each be unique within an inventory. Use the supplied namespace plus logical IDs or provider-generated names; do not use one shared student network.

`RuntimeSnapshot.health` is READY, STARTING, STOPPED, FAILED or UNKNOWN. READY requires every container's actual readiness probe and required access endpoints. `start` must report each resource immediately after allocation, but cleanup must still discover allocations by labels if Core crashed before recording one. After stop, inspect must return STOPPED with no endpoints. After cleanup, inspect must additionally return no resources. If anything remains uncertain, raise an exception or report it; Core retains ERROR and retries rather than declaring success.

Core cancellation is cooperative and deadline-bounded. Poll the cancellation event during slow startup and raise `ProviderCancelled` when acknowledged. On coroutine cancellation or timeout, stop allocating and leave every created resource discoverable. Do not launch detached SDK tasks that can create resources after cleanup completes. Network/volume removals must tolerate already-absent resources and fail on wrong ownership.

Required implementation work remains with #2:

- Per-session network/DNS/IP allocation and cross-session traffic rejection.
- No host networking, Docker socket mounts, global shared training bridge, or unnecessary privileges.
- CPU, RAM, PIDs, writable-root and volume storage enforcement plus host-capacity admission.
- Internet policy implementation (`none` by default), restricted-egress syntax and fail-closed rejection of unsupported policy.
- Image build/pull policy, known image digests and timeouts; Core does not build or pull images.
- `run_as` ownership on ephemeral mounts; `hello-lab` must be able to write `/scratch` as 1000:1000.
- Expiry enforcement while Core is down, using `context.expires_at`; persist it as provider metadata for a watchdog.
- Cleanup of containers, networks, volumes, temporary files, routes and per-attempt secrets.

Acceptance gate: run the same Core/Runtime contract against real Docker, then demonstrate A cannot reach B by network or desktop, readiness is real, unreported allocations are reclaimed, and cleanup leaves no labeled resources. The bundled MemoryRuntime cannot satisfy this gate.

## Astra #3 — desktop and access boundary

Kali and desktop containers are described generically in each manifest; `browser`/`terminal` requirements reference logical containers and ports. Add desktop resources through the Runtime, not through a new Core Lab-specific endpoint.

Coordinate with #2 to return private `EndpointRef(kind="browser"|"terminal", service_ref="opaque-provider-reference")` values in READY inventory. SessionContext passes these references to an injected `AccessBroker.issue(context, principal)` implementation. The broker returns an `AccessGrant` with the same session ID/generation, a short expiry (at most five minutes) and HTTPS/same-origin URLs.

The broker/gateway must bind access to the authenticated student and current generation, reject URL/token reuse by another student, validate every HTTP/WebSocket connection, and revoke existing access on stop, cleanup, reset and expiry. Core validates grant scope and lifetime and refuses issuance for inactive sessions; a URL alone is not gateway authorization.

Lab-specific DNS target names and secret references come from the pinned manifest/environment supplied by #2/#4. Do not embed a scenario target IP or URL into Core or desktop code. #3 owns Kali, XFCE, VNC/noVNC, browser/terminal usability and latency. None are implemented here.

## Astra #5 — frontend integration

Use the supplied OpenAPI contract. Core paths are relative; expose them behind the platform's chosen API prefix through deployment routing. This package does not add a frontend or a browser login flow.

1. Obtain a bearer-authenticated platform identity through the agreed authentication integration.
2. GET `/labs` and render public metadata, `status`, required tools, tasks and interface requirements. No Lab-specific UI route is needed to discover a new manifest.
3. POST `/labs/{id}/sessions` with a unique Idempotency-Key; then POST start.
4. Poll session/status while the operation runs. Acknowledge 202 separately from RUNNING. Show API error messages, not internal exceptions.
5. When RUNNING, GET access and load the #3 gateway URL. Respect short grant expiry and re-request as the gateway contract requires.
6. Read full SessionView for score/tasks/flags. Submit a flag with session generation, flag ID and a stable submission UUID. Core/Validator remain authoritative.
7. Use `expires_at` and `server_time` for the timer. Check `expired` even while a cleanup transition is pending.
8. End Lab through POST cleanup and poll CLEANED. Use stop only for a resumable paused environment. Restart resets the attempt/progress without extending time.

Student APIs never return image names, expected flag values, private config, raw provider IDs or permanent desktop credentials. `progress.completed` is independent of RUNNING/STOPPED. Missing Validator/access adapters appear as unavailable Lab metadata and 503 errors, rather than a simulated success.

Serve frontend and API through an agreed same-origin route by default. This package does not enable permissive CORS, cookies or wildcard trust. If a different-origin frontend is required, request an explicit allowed-origin/authentication deployment decision rather than weakening the HTTP boundary.

## Astra #6 — Validator, results and QA

Supply a mapping `{plugin_id: Validator}` through `--validators-factory module:callable`, or inject it into Engine. The manifest chooses a registered plugin by ID. Its `config`, `scoring`, tasks and private flag references remain available in the trusted context. YAML cannot import arbitrary evaluator code.

The `Validator.submit(context, submission)` contract returns an `Evaluation` absolute snapshot. Own these semantics:

- Expected flags/secrets bound to `(session_id, generation)` and the current Lab.
- Trusted evidence source authentication; student reports do not automatically establish task completion.
- Prerequisites, valid task order, scoring configuration, wrong flags and completion decisions.
- Idempotency for `(session_id, generation, submission_id)`; detect changed payloads on a reused submission ID.
- Strictly increasing evaluator `revision` for each attempt and globally unique result IDs.

Core accepts results only for RUNNING, unexpired, matching session/Lab/generation records. Every Evaluation must include `session_id`; results cannot be copied across two sessions even if they use the same Lab and generation. Core validates references and an arithmetic envelope, persists the exact supplied score/completion snapshot and rejects stale/altered result replays. It does not compare expected flags, calculate task points or infer educational events. The Core initial/reset progress is an empty snapshot, not a scoring policy.

For event-driven evaluation, use the service-authenticated PUT `/internal/sessions/{id}/evaluation`. Internal GET context supplies the current pinned configuration, generation and last accepted progress. The Validator bearer role is a trusted platform service with cross-session access; do not issue it to student desktops or targets. Narrower per-Lab service authorization, if needed, requires an agreed IdentityProvider/policy extension.

Core emits a durable lifecycle feed with `schema_version`, event UUID, global `sequence`, session/Lab IDs, generation, timestamp, type, source and safe data. Event types include SESSION_CREATED, SESSION_STARTING, SESSION_RUNNING, STOP_REQUESTED, SESSION_STOPPING, SESSION_STOPPED, SESSION_RESETTING, SESSION_RESTARTED, SESSION_CLEANING, SESSION_CLEANED, SESSION_ERROR and EVALUATION_UPDATED. Stop intent can be represented by STOP_REQUESTED while status becomes STOPPING. Consumers must process by sequence and payload, not assume every state transition has only one event name.

Poll `/internal/events?after=<checkpoint>&limit=200`. Persist the checkpoint after successful handling and deduplicate by event ID. It is an at-least-once pull contract, not a push webhook or exactly-once broker. Session changes and events are transactional. Educational events such as discovery/authentication must be ingested and validated by #6's trusted event interface; no generic student event endpoint is provided in Core. Raw flag values are excluded from this feed.

QA should extend the included suite with actual Runtime/desktop/scenario/frontend adapters. The Core acceptance test proves that a second manifest works without changing Core files, but does not certify that a real student can complete the whole product.

## Astra #4 — scenario author compatibility

Place the scenario under its own `labs/<id>` directory using the supplied schema. #4 owns target images, wordlists, scenario content, expected credentials and session-bound flags. Coordinate per-attempt secret provisioning with #2/#6 using opaque secret references. No scenario code or answer is introduced into shared Core.

## Integration risks and decisions still required

| Risk / gap | Owner and required resolution |
| --- | --- |
| Existing independently produced modules may use different schemas/routes | All owners: compare these typed contracts before wiring; provide an adapter or explicitly version agreed changes |
| No Docker binary/daemon or infrastructure adapter was available for validation | #2: implement Runtime and run real resource/isolation/cleanup acceptance |
| Database and locks support one control process | Core/platform: shared DB, durable queue and distributed fencing before multi-worker deployment |
| Runtime calls must obey deadlines and cancellation | #2: prove no allocations occur after cancelled start and completed cleanup |
| Core offline cannot itself enforce workload TTL | #2: independent expiry watchdog and gateway expiry enforcement |
| Container storage quota, egress and capacity require actual enforcement | #2: fail unsupported policies and reserve capacity before allocating |
| Public student authentication is a bootstrap token provider | Platform/#5: supply the real IdentityProvider/login integration and token lifecycle |
| Signed desktop access and revocation not implemented | #2/#3: implement AccessBroker + gateway, verify HTTP/WS cross-session rejection |
| Real Validator policy/evidence/flags are absent | #6/#4: implement evaluator and per-attempt secrets; reject forged/out-of-scope evidence |
| SQLite event/session history has no retention job | Core/platform: choose audit retention/archival before sustained traffic; never prune active resources by history age |
| Demo provider resources disappear on process restart | Expected simulation limitation; recovery marks stale sessions and cleans them; real provider state must persist independently |

Intentionally outside Astra #1 scope: frontend UI, Kali image/desktop, VNC/noVNC, Dockerfiles and orchestration implementation, training target, scenario wordlists, Lab-specific Validator or scoring rules, full product deployment and end-to-end student completion certification.
