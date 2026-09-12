from __future__ import annotations
import asyncio
from .errors import CoreError
from .models import Principal, Status, context_for

class SessionReaper:
    def __init__(self, engine, *, occupancy=None, gateway=None, infra=None):
        self.engine = engine
        self.occupancy = occupancy
        self.gateway = gateway
        self.infra = infra

    async def cleanup(self, session_id: str, principal: Principal):
        session = await self.engine.command(session_id, 'cleanup', principal)
        await self.engine.wait_idle(session_id)
        session = self.engine.repo.get(session_id)
        if self.infra is not None:
            state = getattr(self.infra, 'sessions', {}).get(session_id)
            if state is not None and state.get('status') != 'CLEANED':
                stop = getattr(self.infra, 'stop', None)
                if stop is None:
                    raise CoreError('CLEANUP_INCOMPLETE', 'Worker state still contains resources', 503)
                await asyncio.to_thread(stop, session_id, state.get('generation'))
        if self.occupancy is not None:
            self.occupancy.revoke(session_id)
        if self.gateway is not None:
            self.gateway.revoke(session_id)
        leftovers = await self.audit(session)
        if leftovers:
            raise CoreError('CLEANUP_INCOMPLETE', 'Session still owns resources: ' + ', '.join(leftovers), 503)
        return session

    async def audit(self, session):
        leftovers = []
        if session.status == Status.CLEANED:
            if session.resources:
                leftovers.append('session.resources')
            if session.endpoints:
                leftovers.append('session.endpoints')
        snapshot = await self.engine.runtime.inspect(context_for(session))
        if snapshot.resources:
            leftovers.append('runtime.resources')
        if snapshot.endpoints:
            leftovers.append('runtime.endpoints')
        if snapshot.health not in {'STOPPED', 'UNKNOWN'}:
            leftovers.append('runtime.health=' + snapshot.health)
        runtime_store = getattr(self.engine.runtime, 'resources', None)
        if isinstance(runtime_store, dict):
            owned = [item for item in runtime_store.values()
                     if item.labels.get('cyberpod.session') == str(session.session_id)]
            if owned:
                leftovers.append('runtime.store')
        if self.infra is not None:
            state = getattr(self.infra, 'sessions', {}).get(str(session.session_id))
            if state:
                grouped = state.get('resources') or {}
                if any(grouped.get(kind) for kind in ('network', 'volume', 'container')):
                    leftovers.append('infra.resources')
                if session.status == Status.CLEANED and state.get('status') not in {'CLEANED', 'STOPPING'}:
                    leftovers.append('infra.status')
        if self.occupancy is not None:
            if any(record[1] == str(session.session_id) for record in self.occupancy.tickets.values()):
                leftovers.append('desktop.tickets')
        return leftovers

    async def sweep(self):
        dirty = []
        for session in self.engine.repo.all():
            if session.status != Status.CLEANED:
                continue
            if await self.audit(session):
                dirty.append(str(session.session_id))
        return dirty
