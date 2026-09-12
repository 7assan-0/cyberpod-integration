# Repository repair — 2026-09-12

The original public checkout could not run its tests or entrypoint: it omitted Core models, engine, HTTP API, test support and fixture files. The student handler also returned different field names/types than its own test and the companion frontend expected.

## Source provenance and ownership

- Restored missing Core files from the project's `CyberPod-Core-Astra1.zip` (2026-09-11). Existing integration files were retained. The Core engine, repository, schema and bearer API were imported without architectural changes.
- Restored worker source under `infrastructure/` from `CyberPod-Astra2-Infrastructure.zip`; it remains a separate package with its own contracts and 39 tests.
- Restored missing Kali `scripts/`, `packages/` and `config/` from `cyberpod-astra3.zip`, preserving the existing Dockerfile.
- Core-only tests use their original `hello-lab` fixture; product labs are added explicitly by integration suites.

## Repairs

- Added a runnable `cyberpod-integration` entrypoint and a complete cookie/CSRF student adapter under `cyberpod_integration`. Existing `create_student_app` and `ScoreValidator` imports remain compatible.
- Aligned session IDs, generation, revisions, score objects, task prerequisites and flag results with the frontend. Added auth restore, listing, logout, stop/restart and cleanup routes.
- Repeated start keeps its attempt secret; restart rotates it. Stale revisions and another student's access fail. Logout cleans only the authenticated student's sessions.
- Removed fixed Hydra answers from shared Core validator logic and student instructions. Demo validator instances take operator-provided mappings; session-bound evaluation uses manifest task/flag IDs.
- Fixed duplicate simulated resource IDs, destructive-worker restart calls, invented inventory and CLI string error handling. Live translation rejects unsupported policies rather than silently changing requested mounts or shrinking resources.
- Gateway now checks ticket ownership, closes active sockets on revocation/expiry, handles binary frames, preserves query/body/compression, strips control-plane credentials, and requires an operator secret and identity adapter.
- Desktop assets have a separate bounded request budget, so noVNC loading does not block student API polling. Same-origin grants and secure desktop cookies follow deployment settings.
- Forwarded client/HTTPS headers are used only for explicitly trusted proxy addresses.
- Restored real image build inputs; renamed the former placeholder image tags so they cannot overwrite real Kali/target images. Probe ports bind to loopback.

## Verification

- 96 Core, API, gateway and regression tests passed locally.
- 39 worker tests passed locally.
- Student entrypoint help and frontend production build passed.
- Chrome browser simulation checks (desktop/mobile) and the frontend-to-student-API lifecycle passed in GitHub Actions.
- GitHub Actions built the real Kali and training-target images, booted a healthy XFCE/noVNC desktop, reached the target from Kali, rejected a connection to the other test network, and removed test containers afterward.

## Deployment work still required

The container smoke test uses two internal Docker networks on a GitHub runner. It verifies these image and network paths; it does not certify the production worker's firewall configuration or the complete student-to-worker desktop path.

The original worker's `stop` destroys its workloads and ephemeral storage. The adapter can explicitly restart a cleaned worker session, but it cannot preserve a paused desktop's files. A resumable stop interface belongs to the worker contract and remains an integration request.

The real worker also needs reviewed target-secret injection, VNC-secret provisioning, a loopback forwarding adapter for its private bridge endpoints, immutable image references, and the documented host firewall/cgroup configuration. These are component integration gaps; the API simulation and browser simulation identify their modes explicitly.
