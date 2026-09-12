# CyberPod — Astra #3 handoff

Date: 2026-09-11
Component: Kali Linux + browser desktop
Revision: 2 (update of the existing cyberpod-astra3 package)

## What was built

- Kali image definition with an explicit base/MVP package profile.
- Non-root XFCE desktop, terminal, Firefox ESR and the MVP tool executable.
- Foreground TigerVNC, XFCE/DBus and noVNC/websockify process supervision.
- Preserved TCP 6080, secret and target environment interfaces and script paths.
- Authenticated-gateway-ready noVNC wrapper with relative credential/WebSocket
  routes, resize, reconnect, status and fullscreen; original vnc.html retained.
- Browser policies, explicit restricted-proxy support and generic target launcher.
- Readiness checks for live processes, RFB, XFCE and actual noVNC assets.
- Bounded shutdown and ephemeral state; no root/privileged/socket requirement.
- Local component regression tests, repeated process lifecycle checks, and a
  Docker/browser/isolation acceptance harness.
- Resource/security notes and exact integration change requests.

**No OCI image binary was built or pushed in this environment.** The image
definition must be built and validated on a Linux Docker worker. Full CyberPod
browser acceptance is not complete.

## What Astra #1 must provide

Session identity/owner/expiry and generation, plugin target metadata, runtime
lifecycle integration, health gating, authoritative cleanup, authenticated access
decisions, and an adapter from opaque ResourceRecord.metadata to the frontend's
existing desktop.url/status object. Keep Core Lab-agnostic.

## What Astra #2 must provide

Build/publish the tested image digest; allocate one Kali container and isolated
network/home per session; enforce UID 1000, resources, egress and host isolation;
provide per-session file secrets, writable tmpfs ownership, loopback-only
6080 routing and complete cleanup. Host a trusted gateway/static-asset deployment
or coordinate its owner with #1. Close active WebSockets on revocation.

The current infra/v1 schema lacks file-secret mounts and tmpfs uid/gid/mode
fields. These are documented requests, not silent changes to #2's architecture.

## What Astra #5 must provide

Use its existing DesktopEndpoint {url,status} and iframe component. Render the
backend-supplied desktop.html URL once AVAILABLE, show pending/failure states,
preserve exact allowed origins and verify the actual proxy prefix and cookie
flow. No VNC secret handling is required in frontend business logic; the desktop
wrapper fetches it from the trusted authorized gateway.

## Integration risks

1. Core and frontend wire schemas differ; the current Core Docker adapter does
   not enforce this component's required runtime hardening/readiness.
2. Gateway auth/routing is not implemented in the supplied existing packages.
   Static assets must come from a trusted release, not student-controlled HTTP
   on the application's origin.
3. Infra tmpfs/secret contract extensions remain pending.
4. Image package availability, Firefox sandbox compatibility and actual RAM/
   startup/latency require a worker build and real browser tests.
5. Isolation and cleanup across real sessions, expiry and an active WebSocket
   must be verified at platform level. Docker shares the worker kernel.

## Verification actually completed

26 Python tests passed, including ten local process start/stop cycles.
Shell/Python/JavaScript/JSON/XML syntax checks passed.
The service example passed the existing Astra #2 validator and renderer.
Astra #1/#2/#5/#6 original source files were compared byte-for-byte and unchanged.

Docker acceptance was attempted but exited 2 with BLOCKED because Docker is
absent. No real Kali image, GUI, Firefox/target path, cross-session runtime
isolation or resource-destruction claim is made from local fixture results.

See README.md, docs/INTEGRATION.md, docs/TESTING.md and docs/TEST_RESULTS.md.
