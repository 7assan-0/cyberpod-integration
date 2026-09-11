"""Project internal Core models onto the frontend student contract."""
from __future__ import annotations

from .models import Session, Status

FRONTEND_STATUS = {
    Status.CREATED: "CREATED",
    Status.STARTING: "STARTING",
    Status.RUNNING: "RUNNING",
    Status.STOPPING: "STOPPING",
    Status.STOPPED: "STOPPED",
    Status.RESETTING: "STARTING",
    Status.CLEANING: "STOPPING",
    Status.CLEANED: "CLEANED",
    Status.ERROR: "ERROR",
}


def _task_points(task) -> int:
    points = task.extensions.get("points") if task.extensions else None
    if isinstance(points, int) and points >= 0:
        return points
    return 20


def lab_dto(lab, engine) -> dict:
    status, _reason = engine.availability(lab)
    tasks = [{
        "id": task.id,
        "title": task.title,
        "description": task.instructions or task.title,
        "points": _task_points(task),
    } for task in lab.tasks]
    max_score = sum(task["points"] for task in tasks) or 100
    return {
        "id": lab.id,
        "name": lab.name,
        "description": lab.description,
        "difficulty": lab.difficulty,
        "category": "training",
        "estimated_duration_minutes": lab.estimated_duration_minutes,
        "time_limit_seconds": lab.session_timeout_seconds,
        "required_tools": lab.required_tools,
        "status": "published" if status == "AVAILABLE" else status.lower(),
        "objectives": [task.title for task in lab.tasks],
        "tasks": tasks,
        "max_score": int(max_score),
        "instructions": [task.instructions or task.title for task in lab.tasks],
    }


def session_dto(session: Session, engine) -> dict:
    lab = session.definition
    completed = set(session.progress.completed_tasks)
    tasks = []
    previous_done = True
    for task in lab.tasks:
        if task.id in completed:
            state = "COMPLETED"
        elif previous_done:
            state = "AVAILABLE"
        else:
            state = "LOCKED"
        tasks.append({
            "id": task.id,
            "title": task.title,
            "description": task.instructions or task.title,
            "points": _task_points(task),
            "status": state,
        })
        previous_done = task.id in completed
    max_score = int(session.progress.maximum_score or sum(t["points"] for t in tasks) or 100)
    earned = int(session.progress.score)
    flag_states = session.progress.flags
    if any(value == "accepted" for value in flag_states.values()):
        flag_status = "ACCEPTED"
    elif any(value == "rejected" for value in flag_states.values()):
        flag_status = "INCORRECT"
    elif any(value == "pending" for value in flag_states.values()):
        flag_status = "PENDING"
    else:
        flag_status = "NOT_SUBMITTED"
    running = session.status == Status.RUNNING and session.operation is None
    desktop_url = None
    desktop_status = "UNAVAILABLE"
    if session.status == Status.RUNNING:
        desktop_status = "WAITING"
    public_status = FRONTEND_STATUS[session.status]
    if session.progress.completed and public_status == "RUNNING":
        public_status = "COMPLETED"
    return {
        "session_id": str(session.session_id),
        "lab_id": session.lab_id,
        "lab_name": lab.name,
        "revision": session.revision,
        "status": public_status,
        "server_time": engine.clock().isoformat(),
        "created_at": session.created_at.isoformat(),
        "started_at": session.updated_at.isoformat() if session.status != Status.CREATED else None,
        "expires_at": session.expires_at.isoformat(),
        "completion_state": "COMPLETED" if session.progress.completed else "IN_PROGRESS",
        "progress_percent": 0 if not lab.tasks else round(100 * len(completed) / len(lab.tasks)),
        "completed_tasks": len(completed),
        "total_tasks": len(lab.tasks),
        "score": {"earned": earned, "max": max_score},
        "tasks": tasks,
        "flag": {
            "status": flag_status,
            "can_submit": running and not session.progress.completed,
        },
        "capabilities": {
            "can_start": session.status in {Status.CREATED, Status.STOPPED} and session.operation is None,
            "can_stop": session.status in {Status.RUNNING, Status.STARTING, Status.ERROR} or session.operation is not None,
            "can_restart": session.status in {Status.RUNNING, Status.STOPPED, Status.ERROR} and session.operation is None,
        },
        "desktop": {
            "status": desktop_status,
            "url": desktop_url,
            "expires_at": session.expires_at.isoformat() if desktop_status != "UNAVAILABLE" else None,
        },
        "target": {
            "status": "READY" if session.status == Status.RUNNING else
            "STARTING" if session.status in {Status.STARTING, Status.RESETTING} else "UNKNOWN",
        },
        "error": {"code": session.error.code} if session.error else None,
    }
