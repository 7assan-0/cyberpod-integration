import asyncio
from datetime import timedelta
from uuid import uuid4

from cyberpod_core.errors import CoreError
from cyberpod_core.models import AccessGrant, Evaluation, FlagSubmission, Status

from .support import ALICE, BOB, Harness


class StubValidator:
    """External adapter fixture. Fixed result; no educational scoring implementation."""
    def __init__(self):
        self.results = {}

    async def submit(self, context, submission):
        key = (context.session_id, context.generation, submission.submission_id)
        if key not in self.results:
            self.results[key] = Evaluation(
                result_id=uuid4(), session_id=context.session_id, lab_id=context.lab_id, generation=context.generation,
                revision=context.progress.revision + 1, score=37, maximum_score=100,
                completed_tasks=["observe"], flags={"completion": "accepted"}, completed=True)
        return self.results[key]


class StubAccess:
    def __init__(self, clock):
        self.clock = clock
        self.context = None

    async def issue(self, context, principal):
        self.context = context
        return AccessGrant(session_id=context.session_id, generation=context.generation,
                           expires_at=self.clock() + timedelta(seconds=30),
                           browser_url=f"/gateway/sessions/{context.session_id}/attempts/{context.generation}")


class IntegrationTests(Harness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.validator = StubValidator()
        self.engine.validators["test-validator"] = self.validator
        self.edit_manifest(lambda d: d.update(
            validator={"id":"test-validator", "config":{"private":"TEST-PRIVATE"}},
            scoring={"id":"external-policy", "config":{"policy_ref":"fixture"}},
            flags=[{"id":"completion", "label":"Completion flag", "secret_ref":"provider-secret-reference"}]))

    def submission(self, generation=1):
        return FlagSubmission(submission_id=uuid4(), generation=generation,
                              flag_id="completion", value="TEST-PRIVATE-SUBMITTED-FLAG")

    def result(self, session, **changes):
        return Evaluation.model_validate({"result_id": str(uuid4()), "session_id": str(session.session_id), "lab_id": session.lab_id,
                                          "generation": session.generation, "revision": 1,
                                          "score":37, "maximum_score":100, **changes})

    async def test_external_result_is_stored_without_core_score_computation(self):
        s = await self.operate(await self.create(), "start")
        result = await self.engine.submit_flag(str(s.session_id), ALICE, self.submission())
        self.assertEqual(result.progress.score, 37)
        self.assertTrue(result.progress.completed)
        self.assertEqual(result.status, Status.RUNNING)
        events = self.repo.events(0, 200)
        evaluation = events[-1]
        self.assertEqual(evaluation.type, "EVALUATION_UPDATED")
        self.assertEqual(evaluation.source, "validator")
        self.assertNotIn("TEST-PRIVATE-SUBMITTED-FLAG", "".join(e.model_dump_json() for e in events))
        self.assertNotIn("TEST-PRIVATE-SUBMITTED-FLAG", result.model_dump_json())

    async def test_duplicate_submission_and_result_do_not_double_count(self):
        s = await self.operate(await self.create(), "start")
        submission = self.submission()
        first = await self.engine.submit_flag(str(s.session_id), ALICE, submission)
        second = await self.engine.submit_flag(str(s.session_id), ALICE, submission)
        self.assertEqual(second.progress.score, 37)
        self.assertEqual(first.revision, second.revision)
        self.assertEqual(len([e for e in self.repo.events(0, 200) if e.type == "EVALUATION_UPDATED"]), 1)

    async def test_result_id_reuse_with_changed_content_is_rejected(self):
        s = await self.operate(await self.create(), "start")
        result = self.result(s)
        await self.engine.apply_evaluation(str(s.session_id), result)
        changed = result.model_copy(update={"score": 99})
        with self.assertRaises(CoreError) as error:
            await self.engine.apply_evaluation(str(s.session_id), changed)
        self.assertEqual(error.exception.code, "EVALUATION_CONFLICT")

    async def test_out_of_order_result_revision_is_rejected(self):
        s = await self.operate(await self.create(), "start")
        await self.engine.apply_evaluation(str(s.session_id), self.result(s, revision=2))
        with self.assertRaises(CoreError) as error:
            await self.engine.apply_evaluation(str(s.session_id), self.result(s, revision=1))
        self.assertEqual(error.exception.code, "STALE_EVALUATION")

    async def test_wrong_lab_generation_unknown_task_and_unknown_flag_are_rejected(self):
        s = await self.operate(await self.create(), "start")
        for patch, expected in (({"lab_id":"second-lab"}, "EVALUATION_SCOPE"),
                                ({"generation":2}, "EVALUATION_SCOPE"),
                                ({"completed_tasks":["unknown"]}, "UNKNOWN_TASK"),
                                ({"flags":{"unknown":"accepted"}}, "UNKNOWN_FLAG")):
            with self.subTest(patch=patch):
                with self.assertRaises(CoreError) as error:
                    await self.engine.apply_evaluation(str(s.session_id), self.result(s, **patch))
                self.assertEqual(error.exception.code, expected)

    async def test_result_from_another_session_of_same_lab_and_generation_is_rejected(self):
        a = await self.operate(await self.create(), "start")
        b = await self.operate(await self.create(BOB), "start", BOB)
        with self.assertRaises(CoreError) as error:
            await self.engine.apply_evaluation(str(b.session_id), self.result(a))
        self.assertEqual(error.exception.code, "EVALUATION_SCOPE")
        self.assertEqual(self.repo.get(str(b.session_id)).progress.score, 0)

    async def test_flags_are_rejected_before_start_after_stop_and_expiry(self):
        s = await self.create()
        with self.assertRaises(CoreError):
            await self.engine.submit_flag(str(s.session_id), ALICE, self.submission())
        s = await self.operate(s, "start")
        s = await self.operate(s, "stop")
        with self.assertRaises(CoreError):
            await self.engine.submit_flag(str(s.session_id), ALICE, self.submission())
        s = await self.operate(s, "start")
        self.now += timedelta(seconds=601)
        with self.assertRaises(CoreError) as error:
            await self.engine.submit_flag(str(s.session_id), ALICE, self.submission())
        self.assertEqual(error.exception.status, 410)

    async def test_restarting_resets_progress_and_rejects_previous_generation(self):
        s = await self.operate(await self.create(), "start")
        old_result = self.result(s)
        s = await self.engine.apply_evaluation(str(s.session_id), old_result)
        self.assertEqual(s.progress.score, 37)
        s = await self.operate(s, "restart")
        self.assertEqual(s.progress.score, 0)
        self.assertEqual(s.progress.revision, 0)
        with self.assertRaises(CoreError) as error:
            await self.engine.apply_evaluation(str(s.session_id), old_result)
        self.assertEqual(error.exception.code, "EVALUATION_SCOPE")

    async def test_validator_failure_is_sanitized_and_session_remains_running(self):
        class Broken:
            async def submit(self, context, submission):
                raise ValueError("FLAG-SECRETS-AND-CONNECTION-DETAILS")
        self.engine.validators["test-validator"] = Broken()
        s = await self.operate(await self.create(), "start")
        with self.assertRaises(CoreError) as error:
            await self.engine.submit_flag(str(s.session_id), ALICE, self.submission())
        self.assertEqual(error.exception.code, "VALIDATOR_FAILED")
        self.assertNotIn("FLAG-SECRETS", error.exception.message)
        self.assertEqual(self.repo.get(str(s.session_id)).status, Status.RUNNING)
        self.assertEqual(self.repo.get(str(s.session_id)).progress.revision, 0)

    async def test_result_returning_after_stop_is_rejected(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow(StubValidator):
            async def submit(self, context, submission):
                entered.set()
                await release.wait()
                return await super().submit(context, submission)
        self.engine.validators["test-validator"] = Slow()
        s = await self.operate(await self.create(), "start")
        pending = asyncio.create_task(self.engine.submit_flag(str(s.session_id), ALICE, self.submission()))
        await entered.wait()
        await self.operate(s, "stop")
        release.set()
        with self.assertRaises(CoreError) as error:
            await pending
        self.assertEqual(error.exception.code, "SESSION_NOT_RUNNING")
        self.assertEqual(self.repo.get(str(s.session_id)).progress.score, 0)

    async def test_access_grant_is_bound_to_session_generation_and_endpoint_context(self):
        broker = StubAccess(lambda: self.now)
        self.engine.access_broker = broker
        def edit(definition):
            definition["containers"][0]["ports"] = [6080]
            definition["browser"] = {"required":True,"container":"greeting","port":6080}
        self.edit_manifest(edit)
        s = await self.operate(await self.create(), "start")
        grant = await self.engine.access(str(s.session_id), ALICE)
        self.assertEqual(grant.session_id, s.session_id)
        self.assertEqual(grant.generation, s.generation)
        self.assertTrue(broker.context.endpoints)
        with self.assertRaises(CoreError) as error:
            await self.engine.access(str(s.session_id), BOB)
        self.assertEqual(error.exception.status, 404)
        await self.operate(s, "stop")
        with self.assertRaises(CoreError):
            await self.engine.access(str(s.session_id), ALICE)

    async def test_invalid_access_scope_expiry_or_url_is_rejected(self):
        clock = lambda: self.now
        class Wrong(StubAccess):
            patch = {}
            async def issue(self, context, principal):
                grant = await super().issue(context, principal)
                return AccessGrant.model_validate({**grant.model_dump(), **self.patch})
        broker = Wrong(clock)
        self.engine.access_broker = broker
        s = await self.operate(await self.create(), "start")
        for patch in ({"session_id":uuid4()}, {"generation":9},
                      {"expires_at":self.now + timedelta(hours=1)},
                      {"expires_at":self.now}, {"browser_url":"javascript:alert(1)"},
                      {"browser_url":"//other.example"}, {"browser_url":"/\\other.example"}):
            broker.patch = patch
            with self.subTest(patch=patch):
                with self.assertRaises(CoreError) as error:
                    await self.engine.access(str(s.session_id), ALICE)
                self.assertEqual(error.exception.code, "ACCESS_FAILED")
