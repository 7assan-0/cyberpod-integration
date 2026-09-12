import asyncio
import json
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from cyberpod_core.api import create_app
from cyberpod_core.auth import StaticTokens
from cyberpod_core.models import Principal, Status
from cyberpod_core.openapi import document

from .support import ALICE, BOB, ADMIN, Harness, PausingRuntime

TOKEN_A = "a-test-only-token-abcdefghijklmnopqrstuvwxyz"
TOKEN_B = "b-test-only-token-abcdefghijklmnopqrstuvwxyz"
TOKEN_V = "v-test-only-token-abcdefghijklmnopqrstuvwxyz"
TOKEN_ADMIN = "admin-test-only-token-abcdefghijklmnopqrstuvwxyz"


class APITests(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        auth = StaticTokens({TOKEN_A: ALICE, TOKEN_B: BOB, TOKEN_ADMIN: ADMIN,
                             TOKEN_V: Principal(subject="validator-service", scopes={"validator"})})
        self.app = create_app(self.engine, auth, sweep_interval=0, recover=False, own_repository=False)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    async def request(self, method, path, token=TOKEN_A, **kwargs):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        headers.update(kwargs.pop("headers", {}))
        return await self.client.request(method, path, headers=headers, **kwargs)

    async def new(self):
        response = await self.request("POST", "/labs/hello-lab/sessions", json={})
        self.assertEqual(response.status, 201, await response.text())
        return (await response.json())["session_id"]

    async def test_http_acceptance_create_start_poll_stop_cleanup(self):
        response = await self.request("GET", "/labs")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["labs"][0]["id"], "hello-lab")
        sid = await self.new()
        for action, expected in (("start", "RUNNING"), ("stop", "STOPPED"), ("cleanup", "CLEANED")):
            response = await self.request("POST", f"/sessions/{sid}/{action}", json={})
            self.assertEqual(response.status, 202, await response.text())
            self.assertEqual(response.headers["Location"], f"/sessions/{sid}/status")
            await self.engine.wait_idle(sid)
            response = await self.request("GET", f"/sessions/{sid}/status")
            state = await response.json()
            self.assertEqual(state["status"], expected)
            self.assertIn("server_time", state)
        self.assertEqual(self.runtime.resources, {})

    async def test_authentication_required_and_user_header_is_not_identity(self):
        for headers in ({}, {"X-User-ID": "student-a"}, {"Authorization": "Bearer invalid"}):
            response = await self.request("GET", "/labs", token=None, headers=headers)
            self.assertEqual(response.status, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            body = await response.json()
            self.assertEqual(body["error"]["request_id"], response.headers["X-Request-ID"])

    async def test_student_cannot_access_or_mutate_another_session(self):
        sid = await self.new()
        for method, tail in (("GET", ""), ("GET", "/status"), ("GET", "/events"),
                             ("GET", "/access"), ("POST", "/start"), ("POST", "/stop"),
                             ("POST", "/restart"), ("POST", "/cleanup")):
            response = await self.request(method, f"/sessions/{sid}{tail}", token=TOKEN_B)
            self.assertEqual(response.status, 404, await response.text())
        response = await self.request("GET", "/sessions", token=TOKEN_B)
        self.assertEqual((await response.json())["sessions"], [])

    async def test_student_cannot_post_results_or_read_private_context(self):
        sid = await self.new()
        for method, path in (("GET", f"/internal/sessions/{sid}/context"),
                             ("PUT", f"/internal/sessions/{sid}/evaluation"),
                             ("GET", "/internal/events"), ("POST", "/admin/labs/reload")):
            response = await self.request(method, path, json={})
            self.assertEqual(response.status, 403)

    async def test_validator_scope_does_not_grant_student_actions(self):
        sid = await self.new()
        response = await self.request("POST", f"/sessions/{sid}/start", token=TOKEN_V)
        self.assertEqual(response.status, 403)
        response = await self.request("GET", f"/internal/sessions/{sid}/context", token=TOKEN_V)
        self.assertEqual(response.status, 200)
        self.assertIn("definition", await response.json())

    async def test_create_rejects_owner_or_injected_resource_fields(self):
        response = await self.request("POST", "/labs/hello-lab/sessions", json={"owner_id":"student-b"})
        self.assertEqual(response.status, 422)

    async def test_idempotency_and_location_headers(self):
        headers = {"Idempotency-Key": "stable-client-request"}
        a = await self.request("POST", "/labs/hello-lab/sessions", json={}, headers=headers)
        b = await self.request("POST", "/labs/hello-lab/sessions", json={}, headers=headers)
        self.assertEqual(a.status, 201)
        self.assertEqual(b.status, 200)
        self.assertEqual((await a.json())["session_id"], (await b.json())["session_id"])
        self.assertTrue(a.headers["Location"].startswith("/sessions/"))

    async def test_private_config_does_not_leak_in_labs_sessions_or_validation_errors(self):
        self.edit_manifest(lambda d: d["containers"][0].update(
            environment={"PASSWORD":"PRIVATE-MARKER"}, secret_refs={"FLAG":"secret-store-ref"}))
        sid = await self.new()
        for path in ("/labs", "/labs/hello-lab", f"/sessions/{sid}", f"/sessions/{sid}/status"):
            response = await self.request("GET", path)
            text = await response.text()
            for secret in ("PRIVATE-MARKER", "secret-store-ref", "environment", "provider_id", "definition_hash"):
                self.assertNotIn(secret, text)
        response = await self.request("POST", "/labs/hello-lab/sessions", json={"password":"PRIVATE-MARKER"})
        self.assertNotIn("PRIVATE-MARKER", await response.text())

    async def test_openapi_matches_executable_contract(self):
        response = await self.request("GET", "/openapi.json", token=None)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), document())
        response = await self.request("GET", "/schemas/lab.schema.json", token=None)
        self.assertEqual(response.status, 200)
        self.assertIn("containers", (await response.json())["properties"])

    async def test_runtime_routes_and_openapi_paths_match(self):
        actual = set()
        for route in self.app.router.routes():
            if route.method == "HEAD":
                continue
            path = route.resource.canonical
            if "{action}" in path:
                for action in ("start", "stop", "restart", "cleanup"):
                    actual.add((route.method.lower(), path.replace("{action}", action)))
            else:
                actual.add((route.method.lower(), path))
        documented = {(method, path) for path, ops in document()["paths"].items() for method in ops}
        self.assertEqual(actual, documented)

    async def test_demo_health_is_explicitly_simulated(self):
        for path in ("/healthz", "/readyz"):
            response = await self.request("GET", path, token=None)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["execution_mode"], "simulated")

    async def test_invalid_json_nonfinite_numbers_and_body_size(self):
        for data in ("not json", '{"ttl_seconds":NaN}', '{"ttl_seconds":Infinity}'):
            response = await self.request("POST", "/labs/hello-lab/sessions", data=data,
                                          headers={"Content-Type":"application/json"})
            self.assertEqual(response.status, 422)
        response = await self.request("POST", "/labs/hello-lab/sessions", data="{}")
        self.assertEqual(response.status, 415)
        response = await self.request("POST", "/labs/hello-lab/sessions", data="x" * 20000,
                                      headers={"Content-Type":"application/json"})
        self.assertEqual(response.status, 413)

    async def test_bad_ids_paths_and_queries_are_safe(self):
        response = await self.request("GET", "/sessions/not-a-uuid")
        self.assertEqual(response.status, 422)
        response = await self.request("GET", f"/sessions/{uuid4()}")
        self.assertEqual(response.status, 404)
        response = await self.request("GET", "/sessions?limit=100000")
        self.assertEqual(response.status, 422)
        response = await self.request("GET", "/sessions?owner_id=student-b")
        self.assertEqual(response.status, 422)
        response = await self.request("GET", "/missing")
        self.assertEqual(response.status, 404)
        self.assertIn("error", await response.json())

    async def test_admin_reload_adds_another_lab_to_catalog(self):
        import shutil
        from .support import ROOT
        shutil.copytree(ROOT / "tests/fixtures/second-lab", self.path / "labs/second-lab")
        response = await self.request("POST", "/admin/labs/reload", token=TOKEN_ADMIN, json={})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["registered"], 2)
        response = await self.request("GET", "/labs")
        self.assertEqual(len((await response.json())["labs"]), 2)

    async def test_missing_validator_and_access_are_visible_unavailability(self):
        self.edit_manifest(lambda d: d.update(validator={"id":"missing-plugin"}))
        response = await self.request("GET", "/labs/hello-lab")
        data = await response.json()
        self.assertEqual(data["status"], "UNAVAILABLE")
        self.assertEqual(data["unavailable_reason"], "VALIDATOR_UNAVAILABLE")
        response = await self.request("POST", "/labs/hello-lab/sessions", json={})
        self.assertEqual(response.status, 503)

    async def test_event_feed_is_persistent_and_scoped(self):
        sid = await self.new()
        response = await self.request("GET", f"/sessions/{sid}/events?limit=1")
        result = await response.json()
        self.assertEqual(result["events"][0]["type"], "SESSION_CREATED")
        response = await self.request("GET", f'/sessions/{sid}/events?after={result["next_cursor"]}')
        self.assertEqual((await response.json())["events"], [])
        response = await self.request("GET", "/internal/events", token=TOKEN_V)
        self.assertEqual(response.status, 200)

    async def test_client_disconnect_does_not_cancel_lifecycle_worker(self):
        self.use_runtime(PausingRuntime())
        sid = await self.new()
        response = await self.request("POST", f"/sessions/{sid}/start", json={})
        self.assertEqual(response.status, 202)
        await self.runtime.entered.wait()
        response.close()
        self.runtime.release.set()
        await self.engine.wait_idle(sid)
        self.assertEqual(self.repo.get(sid).status, Status.RUNNING)

