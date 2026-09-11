from __future__ import annotations
import hmac, secrets
from uuid import uuid4
from aiohttp import web
from .errors import CoreError
from .models import CreateSession, Evaluation, FlagSubmission, Principal
from .session_secrets import SessionVault

COOKIE = 'cyberpod_session'
USERS = {'demo@cyberpod.local': ('CyberPodDemo123!', Principal(subject='demo-student', scopes={'student'}))}

class ScoreValidator:
    def __init__(self, vault: SessionVault):
        self.vault = vault
        self.seen = {}
    async def submit(self, context, submission):
        key = (context.session_id, context.generation, submission.submission_id)
        if key in self.seen:
            return self.seen[key]
        accepted = self.vault.check(str(context.session_id), context.generation, submission.value)
        result = Evaluation(
            result_id=uuid4(), session_id=context.session_id, lab_id=context.lab_id,
            generation=context.generation, revision=max(context.progress.revision, 0) + 1,
            score=100 if accepted else context.progress.score, maximum_score=100,
            completed=accepted,
            completed_tasks=['crack'] if accepted else list(context.progress.completed_tasks),
            flags={'vault': 'accepted' if accepted else 'rejected'},
        )
        self.seen[key] = result
        return result

def create_student_app(engine, users=USERS, vault=None):
    vault = vault or SessionVault()
    app = web.Application()
    app['engine'] = engine
    app['users'] = users
    app['sessions'] = {}
    app['vault'] = vault
    def current(request):
        record = app['sessions'].get(request.cookies.get(COOKIE))
        if record is None:
            raise CoreError('AUTH_REQUIRED', 'Sign in required', 401)
        if request.method not in {'GET', 'HEAD'} and request.headers.get('X-CSRF-Token') != record['csrf']:
            raise CoreError('CSRF_INVALID', 'CSRF token mismatch', 403)
        return record['principal']
    def payload(session):
        body = {'session': {
            'id': str(session.session_id), 'lab_id': session.lab_id, 'status': session.status.value,
            'revision': session.revision, 'score': session.progress.score,
            'maximum_score': session.progress.maximum_score, 'completed': session.progress.completed,
            'generation': session.generation,
        }}
        dumped = str(body)
        if 'CYBERPOD{' in dumped:
            raise CoreError('INTERNAL_ERROR', 'Secret leaked into student payload', 500)
        return body
    async def login(request):
        body = await request.json()
        record = users.get(body.get('email'))
        if record is None or not hmac.compare_digest(record[0], body.get('password') or ''):
            raise CoreError('INVALID_CREDENTIALS', 'Invalid credentials', 401)
        token, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        app['sessions'][token] = {'principal': record[1], 'csrf': csrf}
        response = web.json_response({'csrf_token': csrf, 'email': body['email']})
        response.set_cookie(COOKIE, token, httponly=True, samesite='Lax')
        return response
    async def labs(request):
        current(request)
        return web.json_response({'labs': [{'id': lab.id, 'name': lab.name} for lab in engine.registry.all()]})
    async def create(request):
        session, _ = await engine.create(request.match_info['lab_id'], current(request), CreateSession(), None)
        return web.json_response(payload(session))
    async def start(request):
        principal = current(request)
        sid = request.match_info['session_id']
        await engine.command(sid, 'start', principal)
        await engine.wait_idle(sid)
        session = engine.get(sid, principal)
        vault.issue(str(session.session_id), session.generation)
        return web.json_response(payload(session))
    async def flags(request):
        principal = current(request)
        sid = request.match_info['session_id']
        body = await request.json()
        session = engine.get(sid, principal)
        if body.get('expected_revision') not in {None, session.revision}:
            raise CoreError('INVALID_STATE', 'Stale session revision', 409)
        updated = await engine.submit_flag(sid, principal, FlagSubmission(
            submission_id=uuid4(), generation=session.generation,
            flag_id=body.get('flag_id') or 'vault', value=body.get('flag') or ''))
        data = payload(updated)
        data['flag'] = 'ACCEPTED' if updated.progress.flags.get('vault') == 'accepted' else 'INCORRECT'
        return web.json_response(data)
    async def cleanup(request):
        principal = current(request)
        sid = request.match_info['session_id']
        await engine.command(sid, 'cleanup', principal)
        await engine.wait_idle(sid)
        vault.drop(sid)
        return web.json_response(payload(engine.get(sid, principal)))
    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except CoreError as exc:
            return web.json_response({'error': {'code': exc.code}}, status=exc.status)
    app.middlewares.append(errors)
    app.router.add_post('/api/v1/auth/login', login)
    app.router.add_get('/api/v1/labs', labs)
    app.router.add_post('/api/v1/labs/{lab_id}/sessions', create)
    app.router.add_post('/api/v1/sessions/{session_id}/start', start)
    app.router.add_post('/api/v1/sessions/{session_id}/flags', flags)
    app.router.add_post('/api/v1/sessions/{session_id}/cleanup', cleanup)
    return app
