"""Session-bound flag adapter; task/flag identities come from the lab manifest."""
import hashlib
from uuid import uuid4
from cyberpod_core.errors import CoreError
from cyberpod_core.models import Evaluation

class ScoreValidator:
    def __init__(self, vault):
        self.vault = vault
        self.seen = {}

    async def submit(self, context, submission):
        key = (str(context.session_id), context.generation, submission.submission_id)
        fingerprint = hashlib.sha256(submission.model_dump_json().encode()).hexdigest()
        if key in self.seen:
            previous, result = self.seen[key]
            if fingerprint != previous:
                raise CoreError('IDEMPOTENCY_CONFLICT', 'Submission ID was reused with different input', 409)
            return result
        accepted = self.vault.check(str(context.session_id), context.generation, submission.value)
        flags = dict(context.progress.flags)
        if flags.get(submission.flag_id) != 'accepted':
            flags[submission.flag_id] = 'accepted' if accepted else 'rejected'
        complete = all(flags.get(flag.id) == 'accepted' for flag in context.definition.flags)
        tasks = [task.id for task in context.definition.tasks]
        maximum = sum(task.extensions.get('points', 0) for task in context.definition.tasks) or 100
        result = Evaluation(
            result_id=uuid4(), session_id=context.session_id, lab_id=context.lab_id,
            generation=context.generation, revision=context.progress.revision + 1,
            score=maximum if complete else context.progress.score, maximum_score=maximum,
            completed=context.progress.completed or complete,
            completed_tasks=tasks if complete else list(context.progress.completed_tasks), flags=flags,
        )
        self.seen[key] = (fingerprint, result)
        return result
