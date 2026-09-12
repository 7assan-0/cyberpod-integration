"""Cookie-authenticated /api/v1 adapter. Core's bearer API remains unchanged."""
import asyncio
import json
from contextlib import suppress
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from aiohttp import web
from pydantic import ValidationError
from cyberpod_core.errors import CoreError
from cyberpod_core.models import CreateSession, FlagSubmission, Principal, Status
from cyberpod_core.student_auth import BrowserSessions, COOKIE, PasswordDirectory
from cyberpod_core.student_dto import lab_dto, session_dto
from cyberpod_core.session_secrets import SessionVault

ENGINE = web.AppKey('student_engine', object)
VAULT = web.AppKey('student_vault', SessionVault)
BROWSER_SESSIONS = web.AppKey('browser_sessions', BrowserSessions)

USERS = {'demo@cyberpod.local': ('CyberPodDemo123!', Principal(subject='demo-student'))}


def create_student_app(engine, users=None, vault=None, *, gateway=None, secure_cookies=False,
                       manage_engine=False, sweep_interval=3):
    users = dict(USERS if users is None else users)
    identities = {email.strip().lower(): value[1] for email, value in users.items()}
    directory = PasswordDirectory({email: value[0] for email, value in users.items()})
    browser_sessions = BrowserSessions()
    vault = vault or SessionVault()
    locks = WeakValueDictionary()

    def lock(sid):
        result = locks.get(sid)
        if result is None:
            result = asyncio.Lock()
            locks[sid] = result
        return result

    @web.middleware
    async def boundary(request, handler):
        try:
            response = await handler(request)
        except CoreError as exc:
            response = web.json_response({'error': {'code': exc.code, 'message': exc.message}}, status=exc.status)
        except (ValueError, TypeError, UnicodeError, ValidationError):
            response = web.json_response({'error': {'code': 'INVALID_REQUEST', 'message': 'Invalid request body'}}, status=422)
        except web.HTTPException as exc:
            response = web.json_response({'error': {'code': f'HTTP_{exc.status}', 'message': exc.reason}}, status=exc.status)
        except Exception as exc:
            import logging
            logging.getLogger('cyberpod.http').error('student_request_failed', extra={'error_type': type(exc).__name__})
            response = web.json_response({'error': {'code': 'INTERNAL_ERROR', 'message': 'Request could not be completed'}}, status=500)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
        return response

    app = web.Application(middlewares=[boundary], client_max_size=16384)
    app[ENGINE], app[VAULT], app[BROWSER_SESSIONS] = engine, vault, browser_sessions

    async def body(request, allowed):
        if request.content_type != 'application/json':
            raise CoreError('CONTENT_TYPE', 'Use application/json', 415)
        data = await request.json()
        if not isinstance(data, dict) or set(data) - set(allowed):
            raise CoreError('INVALID_REQUEST', 'Unexpected request fields', 422)
        return data

    def current(request):
        record = browser_sessions.get(request.cookies.get(COOKIE))
        if record is None:
            raise CoreError('AUTH_REQUIRED', 'Sign in required', 401)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            import hmac
            csrf = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(csrf, record.csrf):
                raise CoreError('CSRF_INVALID', 'CSRF token mismatch', 403)
            origin = request.headers.get('Origin')
            # The frontend and this adapter use a same-origin reverse proxy.
            from urllib.parse import urlsplit
            if origin and (urlsplit(origin).netloc != request.host or urlsplit(origin).scheme not in {'http', 'https'}):
                raise CoreError('ORIGIN_INVALID', 'Origin mismatch', 403)
        return identities[record.email]

    def sid(request):
        return str(UUID(request.match_info['session_id']))

    def payload(session):
        data = {'session': session_dto(session, engine)}
        data['session']['execution_mode'] = 'simulated' if engine.runtime.simulated else 'infrastructure'
        return data

    def user_body(record):
        return {'user': {'id': identities[record.email].subject, 'display_name': record.display_name}, 'csrf_token': record.csrf}

    async def login(request):
        data = await body(request, {'email', 'password'})
        email, password = data.get('email'), data.get('password')
        if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 1024:
            raise CoreError('INVALID_CREDENTIALS', 'Invalid credentials', 401)
        email = email.strip().lower()
        if not await asyncio.to_thread(directory.verify, email, password):
            raise CoreError('INVALID_CREDENTIALS', 'Invalid credentials', 401)
        browser_sessions.revoke(request.cookies.get(COOKIE))
        record = browser_sessions.create(email)
        response = web.json_response(user_body(record))
        response.set_cookie(COOKIE, record.token, httponly=True, secure=secure_cookies, samesite='Lax', path='/', max_age=browser_sessions.ttl)
        return response

    async def me(request):
        current(request)
        return web.json_response(user_body(browser_sessions.get(request.cookies.get(COOKIE))))

    async def logout(request):
        principal = current(request)
        # End the authenticated student's workloads before revoking their cookie.
        for session in engine.repo.all(principal.subject):
            await engine.command(str(session.session_id), 'cleanup', principal)
            await engine.wait_idle(str(session.session_id))
            ended = engine.get(str(session.session_id), principal)
            if ended.status != Status.CLEANED:
                raise CoreError('CLEANUP_INCOMPLETE', 'Could not end the session; retry logout', 503)
            vault.drop(str(session.session_id))
            if gateway:
                gateway.revoke(str(session.session_id))
        browser_sessions.revoke(request.cookies.get(COOKIE))
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    async def labs(request):
        current(request)
        return web.json_response({'labs': [lab_dto(lab, engine) for lab in engine.registry.all()]})

    async def lab(request):
        current(request)
        return web.json_response({'lab': lab_dto(engine.registry.get(request.match_info['lab_id']), engine)})

    async def sessions(request):
        principal = current(request)
        return web.json_response({'sessions': [payload(row)['session'] for row in reversed(engine.repo.all(principal.subject))]})

    async def create(request):
        principal = current(request)
        data = await body(request, {'ttl_seconds'})
        session, created = await engine.create(request.match_info['lab_id'], principal, CreateSession.model_validate(data), request.headers.get('Idempotency-Key'))
        return web.json_response(payload(session), status=201 if created else 200)

    async def status(request):
        return web.json_response(payload(engine.get(sid(request), current(request))))

    async def lifecycle(request):
        principal, session_id = current(request), sid(request)
        await body(request, set())
        action = request.match_info['action']
        async with lock(session_id):
            before = engine.get(session_id, principal)
            if gateway and action in {'stop', 'restart', 'cleanup'}:
                gateway.revoke(session_id)
            await engine.command(session_id, action, principal)
            await engine.wait_idle(session_id)
            session = engine.get(session_id, principal)
            if action == 'restart' and session.generation != before.generation:
                vault.drop(session_id)
            if session.status == Status.CLEANED:
                vault.drop(session_id)
            # Only simulated acceptance uses the in-memory vault. A real runtime
            # provisions its target via the configured Runtime/Validator ports.
            if session.status == Status.RUNNING and engine.runtime.simulated:
                if not vault.target_config(session_id, session.generation):
                    vault.issue(session_id, session.generation)
            return web.json_response(payload(session))

    async def flags(request):
        principal, session_id = current(request), sid(request)
        data = await body(request, {'flag', 'expected_revision', 'flag_id', 'submission_id'})
        async with lock(session_id):
            session = engine.get(session_id, principal)
            revision = data.get('expected_revision')
            if type(revision) is not int or revision != session.revision:
                raise CoreError('INVALID_STATE', 'Stale session revision', 409)
            if not session.definition.flags:
                raise CoreError('FLAG_NOT_FOUND', 'This lab has no flags', 404)
            flag_id = data.get('flag_id') or session.definition.flags[0].id
            submission = FlagSubmission(submission_id=data.get('submission_id') or uuid4(), generation=session.generation,
                                        flag_id=flag_id, value=data.get('flag'))
            if session.progress.completed:
                return web.json_response({**payload(session), 'result': 'ALREADY_ACCEPTED', 'submission_id': str(submission.submission_id)})
            updated = await engine.submit_flag(session_id, principal, submission)
            result = 'ACCEPTED' if updated.progress.flags.get(flag_id) == 'accepted' else 'INCORRECT'
            return web.json_response({**payload(updated), 'result': result, 'submission_id': str(submission.submission_id)})

    async def access(request):
        principal, session_id = current(request), sid(request)
        session = engine.get(session_id, principal)
        if engine.runtime.simulated:
            raise CoreError('DESKTOP_UNAVAILABLE', 'This backend runs API simulation. Use the standalone browser demo or configure a real desktop runtime.', 503)
        grant = await engine.access(session_id, principal)
        return web.json_response(grant.model_dump(mode='json'))

    async def health(request):
        return web.json_response({'status': 'ok', 'execution_mode': 'simulated' if engine.runtime.simulated else 'infrastructure'})

    app.router.add_get('/healthz', health)
    app.router.add_get('/api/v1/auth/me', me)
    app.router.add_post('/api/v1/auth/login', login)
    app.router.add_post('/api/v1/auth/logout', logout)
    app.router.add_get('/api/v1/labs', labs)
    app.router.add_get('/api/v1/labs/{lab_id}', lab)
    app.router.add_post('/api/v1/labs/{lab_id}/sessions', create)
    app.router.add_get('/api/v1/sessions', sessions)
    app.router.add_get('/api/v1/sessions/{session_id}/status', status)
    app.router.add_get('/api/v1/sessions/{session_id}/access', access)
    app.router.add_post('/api/v1/sessions/{session_id}/flags', flags)
    app.router.add_post('/api/v1/sessions/{session_id}/{action:start|stop|restart|cleanup}', lifecycle)
    if gateway:
        gateway.secure_cookies = secure_cookies
        gateway.subject_for_request = lambda request: current(request).subject
        app.router.add_route('*', '/desktop/{session_id}/', gateway.page)
        app.router.add_route('*', '/desktop/{session_id}/{path:.*}', gateway.page)
        async def close_gateway(_):
            await gateway.close()
        app.on_cleanup.append(close_gateway)

    if manage_engine:
        async def monitor():
            while True:
                await asyncio.sleep(sweep_interval)
                try:
                    await engine.sweep_once()
                except Exception as exc:
                    import logging
                    logging.getLogger("cyberpod.core").error("student_monitor_failed", extra={"error_type": type(exc).__name__})
                    continue
                for row in engine.repo.all():
                    if row.status != Status.RUNNING and gateway:
                        gateway.revoke(str(row.session_id))
                    if row.status == Status.CLEANED:
                        vault.drop(str(row.session_id))

        async def lifetime(_):
            task = None
            try:
                await engine.reconcile()
                task = asyncio.create_task(monitor())
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                await engine.close()
                engine.repo.close()
        app.cleanup_ctx.append(lifetime)
    return app
