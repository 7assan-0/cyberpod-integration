# Worker operation and testing

## 1. Prepare a dedicated Linux test worker

Use Docker Engine 28+ (rootful), Docker Compose 2.24+, Python 3.11+, cgroup v2, `iproute2`, `iptables` and `ip6tables`. Use Docker's iptables firewall backend; the `iptables-nft` compatibility command is acceptable, but Docker's native nftables backend is not supported. The daemon must enforce memory/swap/PID/CPU limits and the normal seccomp profile.

Keep Docker firewall management enabled and do not enable host networking, direct routing, nat-unprotected bridge mode or exposed Docker TCP sockets. The worker needs host networking administration because the guard executes in the host namespace. It is a host process, not a privileged Lab container.

On that worker, install the source tree at `/opt/cyberpod/infra` and choose a policy:

```bash
sudo install -d -m 0755 /opt/cyberpod
sudo cp -a infra /opt/cyberpod/infra
sudo install -d -m 0700 /etc/cyberpod-infra /var/lib/cyberpod-infra
sudo install -m 0600 infra/examples/worker.dev.json /etc/cyberpod-infra/worker.json
sudo modprobe br_netfilter
sudo install -m 0644 infra/systemd/cyberpod-infra-sysctl.conf /etc/sysctl.d/90-cyberpod-infra.conf
sudo sysctl -p /etc/sysctl.d/90-cyberpod-infra.conf
```

Make module loading persistent using your distribution's normal mechanism. Do not overwrite existing daemon or firewall configuration. Review `subnet_pool` against host/VPN/VPC routes; a pool covered by a broad route may be exhausted by the conservative collision checks. Select a non-overlapping private range.

The development policy assumes at least 4 CPUs and 6 GiB RAM and permits only `cyberpod/infra-probe:dev`. The production sample assumes 8 CPUs and 16 GiB RAM and starts with an empty image allowlist. Populate production `allowed_images` with reviewed digest references and preload them administratively. The provider never pulls images during Session startup. Pin `BASE_IMAGE` by digest in reproducible image builds; the source Dockerfiles use readable tag defaults for the smoke build and were not built in this environment.

## 2. Build the harmless test image

```bash
cd /opt/cyberpod/infra
sudo docker build -t cyberpod/infra-probe:dev images/probe
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json host-init
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json doctor
```

`host-init` installs only this package's guard chains and references from INPUT/FORWARD/DOCKER-USER. Its prerequisites must pass first. `doctor` thereafter checks worker support and the guard. A missing feature or unexpected firewall chain refuses startup; do not remove the checks to make an incompatible host appear supported.

The optional target foundation can be built separately:

```bash
sudo docker build -t cyberpod/target-runtime:dev images/target-runtime
```

It has no application CMD. Astra #4 must add its target application and readiness endpoint in the Lab-owned image.

## 3. Review generated configuration

Use fresh UUIDs and expiration timestamps. Run these commands as the trusted worker operator. Request/Compose files can contain training secrets, so keep them private and remove temporary input files when no longer needed:

```bash
umask 077
python3 examples/make_request.py > /tmp/cyberpod-request.json
python3 -m cyberpod_infra validate < /tmp/cyberpod-request.json
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json render < /tmp/cyberpod-request.json > /tmp/cyberpod-compose.json
sudo docker compose --env-file /dev/null -f /tmp/cyberpod-compose.json config --quiet
```

`examples/compose.session.example.json` is a static review sample of the generated format. Its network and volume are external; it cannot safely start a complete Session by itself. Use the adapter to create and validate policy and resources.

## 4. Run a session

```bash
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json start --wait < /tmp/cyberpod-request.json
```

The response supplies the Session UUID and generation. Substitute those literal values in these commands:

```bash
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json status SESSION_UUID
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json restart --generation GENERATION_UUID --wait < /tmp/cyberpod-request.json
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json stop SESSION_UUID --generation CURRENT_GENERATION_UUID
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json stop SESSION_UUID --generation CURRENT_GENERATION_UUID
rm /tmp/cyberpod-request.json /tmp/cyberpod-compose.json
```

Restart returns a new generation. Revoke the old proxy route before restart/stop. The fixture responds on the returned private upstream from the worker host; this package does not yet present Kali in a browser because the actual desktop and authenticated proxy are separate dependencies.

## 5. Enable cleanup independent of Core

```bash
sudo install -m 0644 systemd/cyberpod-infra-reap.service /etc/systemd/system/cyberpod-infra-reap.service
sudo install -m 0644 systemd/cyberpod-infra-reap.timer /etc/systemd/system/cyberpod-infra-reap.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cyberpod-infra-reap.timer
sudo python3 -m cyberpod_infra --policy /etc/cyberpod-infra/worker.json reap
```

The unit assumes `/usr/bin/python3` is at least 3.11; adjust its executable if the worker uses a managed virtual environment. Configure host journald retention/monitoring separately. A boot-time reaper will reconcile missing guard rules by cleaning affected existing sessions; new starts install/audit the guard before any workloads execute. Docker restart policies for all Lab containers are disabled.

Inspect structured CLI output and `journalctl -u cyberpod-infra-reap.service`. A cleanup failure must be handled as occupied capacity. Never solve one Session's problem with `docker system prune`, a global volume prune, or a firewall flush.

## 6. Tests

Local deterministic tests, no Docker required:

```bash
python3 -m unittest discover -s tests -v
```

They cover contract rejection, no hidden Docker-option passthrough, egress-policy evaluation, ownership cleanup, failure rollback, readiness, TTL, persistence, restart generations, concurrent admission, cleanup retries, daemon timeout reservations and secret redaction. The fake daemon intentionally does not claim to test Linux networking.

Real lifecycle, network and hardening acceptance on the dedicated worker:

```bash
sudo python3 tests/live_acceptance.py --execute --policy /etc/cyberpod-infra/worker.json --report /tmp/cyberpod-live-report.json
```

This starts two sessions, verifies positive target controls, checks A→B and B→A by direct IP and DNS, tests host gateway denial with a real host listener, checks private worker upstream access, tests non-root/capabilities/read-only storage/ENOSPC, restarts and checks fresh state, kills a target, checks reaping, stops during health startup and verifies repeatable full cleanup. It repeats isolation with routed `restricted`/`internet` sessions so lack of an offline default route cannot explain the result.

To also verify egress, provide a **public IPv4 endpoint you control** with TCP listeners on two distinct ports. The unrestricted session first proves both listeners reachable, then restricted and offline policies must deny the appropriate paths. Do not use an arbitrary third-party target as a test endpoint.

```bash
sudo python3 tests/live_acceptance.py --execute --policy /etc/cyberpod-infra/worker.json --report /tmp/cyberpod-live-report.json --egress-ip YOUR_CONTROLLED_PUBLIC_IPV4 --allowed-port 443 --denied-port 8443
```

The egress fixture also checks public DNS resolution with `example.com`. If policy intentionally blocks that resolution, adapt the test fixture under operator review.

| Harness status / exit | Meaning |
| --- | --- |
| `PASS` / 0 | Real lifecycle, isolation, hardening and configured egress checks passed |
| `PARTIAL` / 1 | Core infrastructure checks passed, but no controlled external fixture was supplied for egress |
| `FAIL` / 1 | A runtime check or final cleanup failed; inspect the report |
| `BLOCKED` / 77 | A required host dependency is missing or execution was not requested; no checks count as passed |

The harness uses `finally` cleanup and reports any leftovers. Run it on a dedicated test worker with sufficient free Session slots. The final file is the evidence for an actual network-isolation claim. Retest on each target host/network configuration before enabling student traffic.

## Troubleshooting and recovery

- `IMAGE_REJECTED`: preload the approved image, confirm digest policy and remove Dockerfile `VOLUME` declarations through the image owner. Do not allow arbitrary image strings from students.
- `SANDBOX_MISMATCH`: inspect the named worker resources as an administrator. The adapter rolls back if actual network, volume or container limits diverge from the request.
- `CAPACITY_EXCEEDED`: inspect active/failed cleanup reservations, memory/CPU capacity, free disk and subnet overlap. Increase limits only after checking host capacity.
- `CONTAINER_UNHEALTHY` or `STARTUP_TIMEOUT`: inspect the image's actual health check and writable-directory requirements. Coordinate image fixes with Astra #3/#4. Do not replace health checks with `true`.
- `CLEANUP_FAILED`: rerun stop/reap. Check busy volume/network attachments and daemon health. A foreign endpoint is not forcibly detached. Timeout holds retain reservations for five minutes; repeated timeouts require draining the worker and investigating Docker.
- `FIREWALL_DRIFT`: stop admissions, stop/reap managed sessions, identify the conflicting firewall change and restore the documented guard on the empty worker. Base-chain content mismatch intentionally refuses automatic rewriting of an unexpected existing policy.
- Lost state: reap finds labeled orphan resources and removes them; fresh admissions are blocked while untracked resources exist. Corrupt state is fail-closed. Preserve a copy, investigate, then move only the corrupt Session state out of the state directory and run reap to remove that Session's labeled resources. Do not infer missing Lab secrets from Docker metadata.
- Controller/worker crash: run reap before admitting students. Verify there are no orphan resources and the health/guard checks pass. State directory files are host-local; copying them to a different worker is not a supported migration.

## Packaging notes

Only `infra/` implementation files should be merged into the project by the responsible owner. The standalone package root README is a handoff note, not a request to overwrite an existing project README. Reaper units, host configuration and firewall initialization are operator actions; no shared application source was changed by this task.
