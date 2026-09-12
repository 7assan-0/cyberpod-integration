# CyberPod Integration

Student `/api/v1` server, the original Astra #1 Core, Astra #2 worker source, and desktop gateway/build inputs. The companion frontend is [cyberpod-demo](https://github.com/7assan-0/cyberpod-demo).

## Local API server

Python 3.12+ is required. Run from this repository:

```bash
python -m venv .venv
```

Activate it on Windows PowerShell with `.\.venv\Scripts\Activate.ps1`, or on Linux/macOS with `source .venv/bin/activate`. Then:

```bash
python -m pip install -e . -e infrastructure
python -m cyberpod_integration --demo
```

The student API listens on **http://127.0.0.1:8000**. Sign in through the companion frontend's `npm run dev:live` with **demo@cyberpod.local / CyberPodDemo123!**. Keep both servers running.

`--demo` runs the real HTTP contract with simulated resources. It does not launch Kali, Docker containers, or a training target. The fully interactive browser simulation is `cyberpod-demo`'s default `npm run dev` mode. For actual infrastructure, supply reviewed runtime, validator and access factories plus a private users file; the command rejects missing configuration and a simulated runtime outside `--demo`.

## Student contract

| Method | Path | Result |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | HttpOnly session cookie, user and CSRF token |
| GET | `/api/v1/auth/me` | Current user and CSRF token |
| POST | `/api/v1/auth/logout` | Clean the student's sessions and revoke the cookie |
| GET | `/api/v1/labs`, `/api/v1/labs/{id}` | Catalog and lab metadata |
| GET | `/api/v1/sessions` | Only the authenticated student's sessions |
| POST | `/api/v1/labs/{id}/sessions` | `{session: …}`; supports `Idempotency-Key` |
| GET | `/api/v1/sessions/{id}/status` | Session, revision, generation, tasks, score and capabilities |
| POST | `/api/v1/sessions/{id}/start`, `stop`, `restart`, `cleanup` | Updated `{session: …}` |
| POST | `/api/v1/sessions/{id}/flags` | `{result, submission_id, session}` |
| GET | `/api/v1/sessions/{id}/access` | Configured desktop grant, or a clear unavailable error |

After login, send `X-CSRF-Token` with mutations and JSON request bodies. Flag submissions require `flag` and the latest `expected_revision`; optional `flag_id` and `submission_id` preserve the Core validator contract. Student session/catalog JSON does not contain target secrets. The original bearer API (`python -m cyberpod_core --demo`) remains available separately and keeps its original paths and schemas.

## Checks

```bash
python -m unittest discover -s tests -t . -v
python -m unittest discover -s infrastructure/tests -v
```

See [docs/REPAIR_REPORT.md](docs/REPAIR_REPORT.md) for restored source provenance, verified behavior and outstanding deployment work. Original Core contracts are under `docs/core/`; worker contracts are under `infrastructure/docs/`. See [GATEWAY.md](GATEWAY.md) and [images/README.md](images/README.md) for the desktop boundary and images.
