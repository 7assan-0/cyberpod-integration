from __future__ import annotations
import secrets, time
from dataclasses import dataclass

@dataclass
class Ticket:
    token: str
    session_id: str
    subject: str
    generation: int
    expires_at: float
    upstream: str

class TicketStore:
    def __init__(self, ttl_seconds: int = 180):
        self.ttl_seconds = min(max(ttl_seconds, 30), 300)
        self._tickets = {}
        self._by_session = {}

    def issue(self, session_id, subject, generation, upstream, now=None):
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        ticket = Ticket(token, session_id, subject, generation, now + self.ttl_seconds, upstream.rstrip('/'))
        self._tickets[token] = ticket
        self._by_session.setdefault(session_id, set()).add(token)
        return ticket

    def resolve(self, token, session_id, now=None):
        now = time.time() if now is None else now
        ticket = self._tickets.get(token)
        if ticket is None:
            raise PermissionError('TICKET_INVALID')
        if ticket.session_id != session_id:
            raise PermissionError('TICKET_SCOPE')
        if now >= ticket.expires_at:
            self._drop(token)
            raise PermissionError('TICKET_EXPIRED')
        return ticket

    def revoke_session(self, session_id):
        tokens = list(self._by_session.get(session_id, ()))
        for token in tokens:
            self._drop(token)
        return len(tokens)

    def revoke_generation(self, session_id, generation):
        count = 0
        for token in list(self._by_session.get(session_id, ())):
            ticket = self._tickets.get(token)
            if ticket and ticket.generation == generation:
                self._drop(token)
                count += 1
        return count

    def _drop(self, token):
        ticket = self._tickets.pop(token, None)
        if ticket is None:
            return
        remaining = self._by_session.get(ticket.session_id, set())
        remaining.discard(token)
        if not remaining:
            self._by_session.pop(ticket.session_id, None)
