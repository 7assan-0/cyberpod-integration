"""Local validator used only in --demo so the student flag contract can be exercised."""
from uuid import uuid4

from .models import Evaluation, FlagSubmission, SessionContext

DEMO_FLAGS = {
    "hydra-ssh-101": "CYBERPOD{hydra_ssh_cracked}",
    "hello-lab": "CYBERPOD{hello}",
}


class LocalDemoValidator:
    async def submit(self, context: SessionContext, submission: FlagSubmission) -> Evaluation:
        expected = DEMO_FLAGS.get(context.lab_id)
        accepted = expected is not None and submission.value == expected
        task_ids = [task.id for task in context.definition.tasks]
        completed = list(task_ids) if accepted else list(context.progress.completed_tasks)
        maximum = 100 if task_ids else 0
        per = maximum // len(task_ids) if task_ids else 0
        score = per * len(completed)
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
