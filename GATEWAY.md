# Desktop Gateway

Protects noVNC HTTP assets and the WebSocket (`/websockify`).

- Ticket bound to session_id + subject + generation
- TTL 30–300 seconds
- Immediate revoke on stop/logout
- Upstream must be loopback only
- `frame-ancestors` set so the frontend can embed the page
- Student A ticket cannot open student B desktop

Core AccessBroker: `cyberpod_gateway.broker.GatewayAccessBroker`
