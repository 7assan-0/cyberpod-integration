# Lab definition contract 1.0

`schemas/lab.schema.json` is generated from `LabDefinition` and is the authoritative machine-readable field/type contract. `labs/hello-lab/lab.yaml` is a complete runnable example for a compatible real Runtime, and is executed as a simulation by the bundled demo provider.

| Field | Purpose / constraints |
| --- | --- |
| `schema_version` | Literal `"1.0"`; unknown versions fail validation |
| `id` | Lowercase slug, up to 63 characters; must match directory name |
| `name`, `description` | Public display metadata |
| `version` | Lab content version, pinned in each session |
| `enabled` | Controls availability for new sessions; default true |
| `difficulty` | beginner, intermediate, advanced or expert |
| `estimated_duration_minutes` | Public duration estimate, 1–1440 |
| `session_timeout_seconds` | Server-enforced maximum TTL, 60–86400; default 3600 |
| `required_tools` | Public list; does not execute installation commands in Core |
| `containers` | 1–32 container specifications with logical IDs |
| `networks` | 1–16 session-scoped network specifications |
| `volumes` | Ephemeral session volumes; no host-source paths |
| `resources` | Total session CPU, RAM, process and storage budget |
| `tasks` | Public task IDs, titles, instructions and dependency references |
| `validator` | Registered plugin ID and private JSON configuration, or null |
| `scoring` | Private policy plugin ID/configuration for the Validator, or null |
| `flags` | Public ID/label plus optional private secret reference |
| `browser`, `terminal` | Required flag, logical container, port, protocol and path |
| `extensions` | Explicit JSON extension data; keep keys namespaced, for example `org.example.feature` |

## Containers, networks and resources

A container describes `id`, `image`, role metadata, argument-vector `command`, private environment values and secret references, logical network references, internal ports, ephemeral volume mounts, per-container limits, readiness command, non-host user/group, and root filesystem mode. Core does not interpret a particular tool or target image. It does not execute YAML command strings; the Runtime owns container execution.

Network `scope` is always `session`. Internet policy is `none`, `restricted`, or `allowed`. Only `restricted` must include an `egress_allowlist`. The Runtime defines and documents its supported allowlist syntax and rejects unsupported policy. Addresses, subnets and provider container names are absent from the generic schema. Declared ports are internal ports, not host-published ports.

Resource limits use CPU units, MiB RAM, PIDs and MiB writable storage. The schema verifies the sum of container budgets fits within the session budget; volume storage is also counted. Infrastructure must enforce those limits and perform admission against real host capacity. Schema acceptance is not proof of cgroup, filesystem quota or egress enforcement.

Volumes are exclusively session-owned and ephemeral. `size_mb` is an enforceable requested cap, not permission to create an unlimited Docker volume. The infrastructure adapter must provision writable mount permissions for each container's `run_as` UID/GID. In `hello-lab`, UID/GID 1000 must be able to write `/scratch`, despite the read-only container root filesystem.

Every container requires a readiness healthcheck. The Runtime must distinguish an allocated/running container from a ready workload, honor probe interval/timeout/retries, and return READY only when all required resources/endpoints pass. `hello-lab` writes a marker to its volume and uses that marker for its healthcheck; the actual image and volume behavior remain an Astra #2 integration test.

## Validation and extension behavior

IDs must be unique within each resource/task group. Container networks, mounts, access container/port references and task dependencies must exist. Task dependencies must be acyclic. Core validates the graph structure only; Astra #6 enforces educational ordering and scoring.

Unknown structural keys are rejected so typos do not silently change behavior. Use explicit `extensions` for additional metadata. An adapter must reject unsupported operational extensions instead of ignoring them. The demo runtime rejects operational extensions because it cannot implement their semantics. A new schema version is required for incompatible contract changes.

Manifest input is trusted operator configuration, never student upload. Files must be UTF-8, at most 256 KiB, one level under the configured Lab root. Duplicate YAML mapping keys, aliases/anchors, executable YAML tags and symlinked manifests/directories are rejected. An invalid manifest rejects the entire reload while preserving the previous valid registry.

Public APIs intentionally omit container specifications, environment values, secret references, Validator configuration, scoring configuration, and arbitrary extensions. Task instructions and Lab descriptions are public by design; scenario authors must not put answers in those fields.

## Add another Lab without changing Core

1. Add `labs/<new-id>/lab.yaml` using schema version 1.0. Keep the new ID equal to the directory name.
2. Supply scenario assets/images through Astra #4 and infrastructure support through Astra #2.
3. If needed, register a Validator implementation by the manifest's plugin ID and configure the AccessBroker. Registration happens in trusted application/factory code, not YAML imports.
4. Call `POST /admin/labs/reload` with an admin identity, or restart Core.
5. Confirm the Lab appears in `GET /labs`; create/start/stop/cleanup through the same endpoints as any other Lab.

The fixture in `tests/fixtures/second-lab` exercises this contract. Copying that directory into the configured Lab root is sufficient to register it; no switch statement, router change or new Lab-specific Core function is involved.

Disabling/removing a manifest prevents new sessions. Existing sessions retain their pinned definition, so they can still be inspected, stopped, resumed or cleaned. To stop existing sessions operationally, issue lifecycle commands; registry reload does not silently rewrite them.

