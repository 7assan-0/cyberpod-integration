from __future__ import annotations
import secrets, time
from .errors import CoreError
from .models import Principal, Session

class Occupancy:
    def __init__(self):
        self.sessions = {}
        self.tickets = {}

    def occupy(self, session: Session) -> None:
        sid = str(session.session_id)
        current = self.sessions.get(sid)
        if current and current != session.owner_id:
            raise CoreError('FORBIDDEN', 'Session occupancy conflict', 403)
        self.sessions[sid] = session.owner_id

    def visible(self, sessions, principal: Principal):
        if 'admin' in principal.scopes:
            return sessions
        return [session for session in sessions if session.owner_id == principal.subject]

    def require_owner(self, session: Session, principal: Principal):
        if session.owner_id != principal.subject and 'admin' not in principal.scopes:
            raise CoreError('SESSION_NOT_FOUND', 'Session not found', 404)
        return session

    def issue_ticket(self, session: Session, principal: Principal, generation: int, ttl: int = 180) -> str:
        self.require_owner(session, principal)
        token = secrets.token_urlsafe(32)
        self.tickets[token] = (principal.subject, str(session.session_id), generation, time.time() + ttl)
        return token

    def resolve_ticket(self, token: str, session_id: str, principal: Principal | None = None):
        record = self.tickets.get(token)
        if record is None:
            raise CoreError('FORBIDDEN', 'Desktop ticket rejected', 403)
        owner, bound, generation, expires = record
        if bound != session_id or time.time() >= expires:
            raise CoreError('FORBIDDEN', 'Desktop ticket rejected', 403)
        if principal is not None and principal.subject != owner:
            raise CoreError('FORBIDDEN', 'Desktop ticket rejected', 403)
        return owner, generation

    def revoke(self, session_id: str) -> int:
        self.sessions.pop(session_id, None)
        drop = [token for token, record in self.tickets.items() if record[1] == session_id]
        for token in drop:
            self.tickets.pop(token, None)
        return len(drop)
