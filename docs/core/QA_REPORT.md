# Core validation report — 2026-09-11

Scope: Astra #1 Core package, version 0.1.0, integration contract 1.0.

## Executed results

**70 tests passed, 0 failed, 0 skipped.** Latest complete suite: 1.857 seconds in this environment. Expected fault-injection tests emit sanitized `runtime_failure`/`runtime_unhealthy` log records; these are intentional test scenarios, not failed test cases.

| Suite | Passed | Coverage |
| --- | ---: | --- |
| Core lifecycle | 30 | Discovery, full lifecycle, second Lab, owner/resource isolation, concurrency, idempotency, quotas, expiry, recovery, failed startup/cleanup, health and restart |
| HTTP API | 17 | Actual loopback HTTP requests, bearer roles, cross-student access, protected callbacks/context, input limits, public-field filtering, routes/contracts, catalog reload and lifecycle polling |
| Schema/contracts | 11 | Invalid YAML, duplicate keys, aliases, symlinks, size limits, atomic registry reload, extension behavior, schema references and export drift |
| External interfaces | 12 | Absolute evaluation persistence, session/Lab/generation binding, result replay, stale results, Validator failure/races, short-lived access scope and URL checks |
| **Total** | **70** | All pass |

Command:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v
```

The complete last run is retained in `docs/test-results.txt`.

Additional executed checks:

- Python source/tests compiled successfully.
- The package built and installed into an isolated target directory using `pip --no-index --no-deps --no-build-isolation`; no dependency downloads were used.
- The installed CLI launched on an ephemeral loopback port, using its installed package rather than the source import path.
- Actual HTTP calls completed CREATED → RUNNING → STOPPED → CLEANED, with asynchronous acknowledgments and polling.
- Graceful CLI shutdown returned exit code zero with no traceback.

Environment: Linux, Python 3.12.14, aiohttp 3.13.5, Pydantic 2.13.5, PyYAML 6.0.3. Those runtime dependency versions were already installed. A fresh online dependency installation was not verified because package-network access was unavailable. The documented Windows commands were not executed on Windows.

## Mandatory Lab extensibility acceptance

`test_mandatory_hello_lifecycle_and_configuration_only_second_lab` passed all required steps:

1. Discover `hello-lab` from its directory.
2. Validate/read its container, network and volume configuration.
3. Create a session.
4. Start through the Runtime contract and reach RUNNING.
5. Stop and confirm STOPPED while retaining resumable resources.
6. Clean and verify an empty provider inventory.
7. Copy an independently named second manifest, reload, create/start/stop/clean it and verify unchanged Core source hashes.

The provider for this acceptance test is explicitly simulated. The test validates Core behavior and provider ownership contracts; it does not establish container execution, egress restrictions, cgroup enforcement or physical network isolation.

## Boundary assessment

| Area | Result | Meaning |
| --- | --- | --- |
| Core | PASS | Executed Core/API/schema checks |
| Infrastructure | NOT RUN | Real Runtime/Docker adapter and daemon absent |
| Kali desktop | NOT RUN | Owned by Astra #3; no desktop implementation in this package |
| Training scenario | NOT RUN | Owned by Astra #4; no scenario content in Core |
| Frontend | NOT RUN | Owned by Astra #5; no UI in this package |
| Validator | CONTRACT PASS / REAL RULES NOT RUN | Core adapter/callback protections pass; educational rules belong to #6 |
| Product end-to-end | NOT RUN | Login → real desktop → scenario → actual score requires all component implementations |
| Cleanup | CORE CONTRACT PASS / DOCKER NOT RUN | Idempotent/failed/partial cleanup tested using owned simulated resources |
| Lab extensibility | CORE PASS | Second Lab added without any Core code modification |

## Known integration risks

No failing in-scope test remains. Integration is not complete until the actual component owners implement and verify their ports. Required follow-ups are detailed in `INTEGRATION.md`: real isolation and storage/egress enforcement, gateway authorization/revocation, offline TTL watchdog, trusted evidence and per-attempt flags, production identity integration, and contract compatibility with the other Astra packages.

The SQLite implementation intentionally enforces one control process. It is not a multi-worker or multi-host architecture. A sustained deployment also needs an agreed history-retention policy and storage/backup operations. None of the Core test results should be reported as a production penetration test or a whole-product PASS.

