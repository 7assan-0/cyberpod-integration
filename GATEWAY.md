# Desktop gateway

`DesktopGateway` proxies noVNC assets and binary WebSockets at `/desktop/{session_id}/…`.

- A ticket is bound to session, subject and generation, and expires after 30–300 seconds.
- Every HTTP request and active WebSocket is checked. Revocation closes existing sockets; a new generation invalidates old tickets.
- The authenticated browser subject must match the ticket owner. `create_student_app(..., gateway=...)` wires this to the student session cookie and mounts the gateway on the same origin. A separately hosted gateway must supply its own verified `subject_for_request` adapter; it fails closed when this is missing.
- The default browser URL is relative to the frontend origin. When mounted on the student API, desktop cookies inherit its `secure_cookies` deployment setting. A standalone gateway can set this explicitly when TLS terminates at a trusted proxy.
- Upstreams must be operator-registered loopback HTTP(S) services. Credentials and student control-plane cookies/Authorization are never forwarded upstream.
- `gateway.register(session_id, upstream, password=...)` accepts the attempt's VNC password through the trusted runtime path. The private `/credentials` endpoint serves it only after cookie and ticket verification; it is excluded from student session JSON.
- `create_app` requires a private operator token of at least 24 characters, either explicitly or through `CYBERPOD_GATEWAY_OPERATOR_TOKEN`. There is no default operator password.
- Use a relative `public_base=''` or the frontend HTTPS origin for `GatewayAccessBroker`; the frontend uses same-origin frames and requests. The operator must configure any reverse proxy explicitly.

The worker publishes private bridge endpoints, while this gateway accepts loopback upstreams. A trusted worker-side forwarding adapter and VNC-secret provisioning are still required for actual Kali sessions. The API simulation reports desktop unavailability clearly and does not pretend to launch Kali.
