"""Local validator used only in --demo so the student flag contract can be exercised."""
from uuid import uuid4

from .models import Evaluation, FlagSubmission, SessionContext



class LocalDemoValidator:
    def __init__(self, flags=None):
        self.flags = dict(flags or {})

    async def submit(self, context: SessionContext, submission: FlagSubmission) -> Evaluation:
        expected = self.flags.get(context.lab_id)
        accepted = expected is not None and submission.value == expected
        task_ids = [task.id for task in context.definition.tasks]
        completed = list(task_ids) if accepted else list(context.progress.completed_tasks)
        maximum = 100 if task_ids else 0
        score = maximum if accepted else context.progress.score
        flags = dict(context.progress.flags)
        flags[submission.flag_id] = "accepted" if accepted else "rejected"
        return Evaluation(
            result_id=uuid4(),
            session_id=context.session_id,
            lab_id=context.lab_id,
            generation=context.generation,
            revision=max(context.progress.revision + 1, 1),
            score=score,
            maximum_score=maximum,
            completed=accepted,
            completed_tasks=completed,
            flags=flags,
        )
