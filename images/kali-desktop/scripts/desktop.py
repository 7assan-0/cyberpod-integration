#!/usr/bin/env python3
"""One desktop process tree. No Docker, Core, target or scoring dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path('/opt/cyberpod')
METADATA = ('CYBERPOD_SESSION_ID', 'CYBERPOD_LAB_ID', 'CYBERPOD_TARGET_HOST',
            'CYBERPOD_TARGET_PORT', 'CYBERPOD_TARGET_URL')
SECRETS = ('CYBERPOD_VNC_PASSWORD', 'CYBERPOD_VNC_PASSWORD_FILE')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def integer(env, name, default, lower, upper):
    value = env.get(name, str(default))
    if not re.fullmatch(r'[0-9]{1,5}', value):
        raise ValueError(f'{name} must be decimal digits')
    result = int(value, 10)
    if not lower <= result <= upper:
        raise ValueError(f'{name} is outside {lower}..{upper}')
    return result


def http_url(value):
    if len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError('target URL contains whitespace/control characters or is too long')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or '\\' in value or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
    except ValueError:
        raise ValueError('target URL must be an HTTP(S) URL without credentials') from None
    return value


@dataclass(frozen=True)
class Config:
    session: str
    home: Path
    port: int
    display: int
    width: int
    height: int
    depth: int
    shared: bool
    metadata: dict

    @classmethod
    def from_env(cls, env):
        session = env.get('CYBERPOD_SESSION_ID', '')
        if not IDENTIFIER.fullmatch(session):
            raise ValueError('CYBERPOD_SESSION_ID must match the infra/v1 identifier format')
        lab = env.get('CYBERPOD_LAB_ID', '')
        if lab and not IDENTIFIER.fullmatch(lab):
            raise ValueError('CYBERPOD_LAB_ID is invalid')
        metadata = {k: env[k] for k in METADATA if env.get(k)}
        host = metadata.get('CYBERPOD_TARGET_HOST', '')
        if host and (len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+', host)):
            raise ValueError('CYBERPOD_TARGET_HOST is invalid')
        if metadata.get('CYBERPOD_TARGET_PORT'):
            integer(env, 'CYBERPOD_TARGET_PORT', 80, 1, 65535)
        if metadata.get('CYBERPOD_TARGET_URL'):
            http_url(metadata['CYBERPOD_TARGET_URL'])
        port = integer(env, 'CYBERPOD_NOVNC_PORT', 6080, 1024, 65535)
        display = integer(env, 'CYBERPOD_VNC_DISPLAY', 1, 1, 99)
        if port == 5900 + display:
            raise ValueError('noVNC and VNC ports must differ')
        depth = integer(env, 'CYBERPOD_DESKTOP_DEPTH', 24, 16, 32)
        if depth not in (16, 24, 32):
            raise ValueError('CYBERPOD_DESKTOP_DEPTH must be 16, 24 or 32')
        shared = env.get('CYBERPOD_VNC_SHARED', 'false')
        if shared not in ('true', 'false'):
            raise ValueError('CYBERPOD_VNC_SHARED must be true or false')
        return cls(session, Path(env.get('HOME', '/home/student')), port, display,
                   integer(env, 'CYBERPOD_DESKTOP_WIDTH', 1440, 800, 3840),
                   integer(env, 'CYBERPOD_DESKTOP_HEIGHT', 900, 600, 2160),
                   depth, shared == 'true', metadata)

    @property
    def state(self):
        return self.home / '.config/cyberpod'


def read_password(env):
    if env.get('CYBERPOD_VNC_PASSWORD_FILE'):
        with open(env['CYBERPOD_VNC_PASSWORD_FILE'], 'rb') as stream:
            raw = stream.read(257)
        try:
            value = raw.decode('ascii').removesuffix('\n').removesuffix('\r')
        except UnicodeError:
            raise ValueError('VNC secret must be printable ASCII') from None
    else:
        value = env.get('CYBERPOD_VNC_PASSWORD', '')
    if not 8 <= len(value) <= 128 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('VNC secret must have 8..128 non-whitespace ASCII characters')
    # Classic VNC consumes only the first eight bytes. Never log the value.
    return value


def child_env(env, config):
    clean = dict(env)
    for name in (*SECRETS, 'SESSION_MANAGER', 'DBUS_SESSION_BUS_ADDRESS'):
        clean.pop(name, None)
    clean.update(DISPLAY=f':{config.display}',
                 XAUTHORITY=str(config.state / 'Xauthority'),
                 XDG_RUNTIME_DIR=f'/tmp/cyberpod-runtime-{os.getuid()}')
    return clean


def private_write(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


def browser_preferences(env, config):
    prefs = {'browser.startup.homepage': 'about:blank',
             'browser.startup.page': 0, 'browser.sessionstore.resume_from_crash': False,
             'network.proxy.type': 0}
    # Firefox does not reliably consume HTTP_PROXY itself. Mirror the trusted
    # infrastructure proxy settings into this fresh, disposable user profile.
    proxies = {kind: env.get(kind.upper() + '_PROXY') or env.get(kind + '_proxy')
               for kind in ('http', 'https')}
    if any(proxies.values()):
        prefs['network.proxy.type'] = 1
        for kind, url in proxies.items():
            if not url:
                continue
            parsed = urlsplit(http_url(url))
            if parsed.scheme != 'http' or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                raise ValueError('desktop proxy must be an HTTP proxy origin')
            key = 'http' if kind == 'http' else 'ssl'
            prefs[f'network.proxy.{key}'] = parsed.hostname
            prefs[f'network.proxy.{key}_port'] = parsed.port or 80
        bypass = env.get('NO_PROXY', env.get('no_proxy', 'localhost,127.0.0.1,::1'))
        if any(ord(c) < 32 for c in bypass):
            raise ValueError('NO_PROXY contains control characters')
        prefs['network.proxy.no_proxies_on'] = bypass
    return ''.join(f'user_pref({json.dumps(k)}, {json.dumps(v)});\n' for k, v in prefs.items())


def prepare_files(config, env, root=ROOT):
    config.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.state.chmod(0o700)
    marker = config.state / 'session-id'
    if marker.exists() and marker.read_text() != config.session:
        raise ValueError('student home belongs to another session; fresh storage required')
    private_write(marker, config.session)
    private_write(config.state / 'session.env', ''.join(
        f'export {k}={shlex.quote(v)}\n' for k, v in config.metadata.items()))
    private_write(config.state / 'session.json', json.dumps(config.metadata, indent=2) + '\n')
    rc = config.home / '.bashrc'
    line = '[ -f "$HOME/.config/cyberpod/session.env" ] && . "$HOME/.config/cyberpod/session.env"'
    content = rc.read_text() if rc.exists() else '# CyberPod student shell\n'
    if line not in content:
        private_write(rc, content + '\n' + line + '\n')
    xfce = config.home / '.config/xfce4'
    xfce.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / 'config/xfce/helpers.rc', xfce / 'helpers.rc')
    channels = xfce / 'xfconf/xfce-perchannel-xml'
    channels.mkdir(parents=True, exist_ok=True)
    for source in (root / 'config/xfce/xfconf').glob('*.xml'):
        shutil.copyfile(source, channels / source.name)
    firefox = config.home / '.mozilla/firefox'
    profile = firefox / 'cyberpod.default'
    profile.mkdir(parents=True, exist_ok=True)
    private_write(firefox / 'profiles.ini', '[General]\nStartWithLastProfile=1\n'
                  '[Profile0]\nName=cyberpod\nIsRelative=1\nPath=cyberpod.default\nDefault=1\n')
    private_write(profile / 'user.js', browser_preferences(env, config))
    desktop = config.home / 'Desktop'
    desktop.mkdir(exist_ok=True)
    shortcut = desktop / 'Lab Target.desktop'
    # Exec is constant. URL data is read as JSON by a launcher, never interpolated
    # into shell/desktop Exec syntax. Entries are also available in XFCE menus.
    if config.metadata.get('CYBERPOD_TARGET_URL'):
        private_write(shortcut, '[Desktop Entry]\nType=Application\nName=Lab Target\n'
                      'Exec=/opt/cyberpod/bin/open-target.sh\nIcon=web-browser\nTerminal=false\n')
        shortcut.chmod(0o700)
    else:
        shortcut.unlink(missing_ok=True)


def rfb_banner(port):
    with socket.create_connection(('127.0.0.1', port), timeout=0.6) as conn:
        conn.settimeout(0.6)
        banner = b''
        while len(banner) < 12:
            part = conn.recv(12 - len(banner))
            if not part:
                break
            banner += part
        if not re.fullmatch(rb'RFB 003\.00[378]\n', banner):
            raise RuntimeError('VNC is not speaking RFB')


def window_manager(env):
    result = subprocess.run(['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'], env=env,
                            capture_output=True, text=True, timeout=1)
    if result.returncode or not re.search(r'0x[1-9a-fA-F][0-9a-fA-F]*', result.stdout):
        raise RuntimeError('XFCE window manager is not ready')


def health(config, env):
    # No target requests: a target failing belongs to Lab health, not desktop health.
    state = json.loads((config.state / 'ready.json').read_text())
    if state['session_id'] != config.session:
        raise RuntimeError('readiness session mismatch')
    for pid in state['pids']:
        os.kill(int(pid), 0)
    rfb_banner(5900 + config.display)
    window_manager(env)
    # Bypass session HTTP_PROXY and require actual noVNC content, not just TCP.
    opener = build_opener(ProxyHandler({}))
    for path in ('vnc.html', 'core/rfb.js'):
        with opener.open(f'http://127.0.0.1:{config.port}/{path}', timeout=0.8) as reply:
            if reply.status != 200 or not reply.read(32):
                raise RuntimeError('noVNC assets unavailable')


class Supervisor:
    def __init__(self, env, grace=5.0):
        self.env = env
        self.grace = grace
        self.processes = []
        self.stopping = False

    def spawn(self, name, args):
        proc = subprocess.Popen(args, env=self.env, start_new_session=True)
        self.processes.append((name, proc))
        return proc

    def check(self):
        if self.stopping:
            raise InterruptedError('desktop stopping')
        for name, proc in self.processes:
            if proc.poll() is not None:
                raise RuntimeError(f'{name} exited unexpectedly ({proc.returncode})')

    def until(self, check, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            try:
                check()
                return
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise RuntimeError('desktop readiness deadline exceeded') from None
                time.sleep(0.15)

    def stop(self):
        # Kill groups even when the group leader has already died. XFCE/DBus
        # children otherwise survive a foreground service failure.
        for _, proc in reversed(self.processes):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + self.grace
        for _, proc in reversed(self.processes):
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for _, proc in reversed(self.processes):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def run(config, env):
    if os.getuid() != 1000:
        raise ValueError('desktop requires user 1000:1000; set the infra service user explicitly')
    password = read_password(env)
    clean = child_env(env, config)
    for name in SECRETS:
        os.environ.pop(name, None)
    prepare_files(config, clean)
    runtime = Path(clean['XDG_RUNTIME_DIR'])
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    for directory in ('/tmp/.X11-unix', '/tmp/.ICE-unix'):
        path = Path(directory)
        if not path.exists():
            path.mkdir(mode=0o1777)
            path.chmod(0o1777)
    vnc_dir = config.home / '.config/tigervnc'
    vnc_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    passwd = vnc_dir / 'passwd'
    result = subprocess.run(['tigervncpasswd', '-f'], input=(password + '\n').encode(),
                            capture_output=True, env=clean, check=True, timeout=5)
    password = None
    if len(result.stdout) != 8:
        raise RuntimeError('VNC password encoder returned unexpected output')
    passwd.write_bytes(result.stdout)
    passwd.chmod(0o600)
    # Feed the X authority cookie on stdin, not in a process command line.
    private_write(Path(clean['XAUTHORITY']), '')
    subprocess.run(['xauth', '-f', clean['XAUTHORITY']],
                   input=f'add :{config.display} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n',
                   text=True, check=True, env=clean, timeout=5, stdout=subprocess.DEVNULL)
    ready = config.state / 'ready.json'
    ready.unlink(missing_ok=True)
    supervisor = Supervisor(clean)
    def stop_signal(_number, _frame):
        supervisor.stopping = True
    old_handlers = {s: signal.signal(s, stop_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    started = time.monotonic()
    try:
        supervisor.spawn('vnc', ['Xtigervnc', f':{config.display}', '-localhost', 'yes',
            '-rfbport', str(5900 + config.display), '-SecurityTypes', 'VncAuth',
            '-PasswordFile', str(passwd), '-auth', clean['XAUTHORITY'], '-nolisten', 'tcp',
            '-geometry', f'{config.width}x{config.height}', '-depth', str(config.depth),
            '-desktop', f'CyberPod {config.session}', '-FrameRate', '30',
            '-AlwaysShared' if config.shared else '-NeverShared'])
        supervisor.until(lambda: rfb_banner(5900 + config.display), 20)
        supervisor.spawn('xfce', [str(ROOT / 'config/xfce/xstartup')])
        supervisor.until(lambda: window_manager(clean), 40)
        supervisor.spawn('websockify', ['websockify', '--web=/usr/share/novnc',
                                       f'0.0.0.0:{config.port}', f'127.0.0.1:{5900 + config.display}'])
        private_write(ready, json.dumps({'session_id': config.session,
                       'pids': [p.pid for _, p in supervisor.processes]}))
        supervisor.until(lambda: health(config, clean), 10)
        print(json.dumps({'component': 'kali-desktop', 'state': 'READY',
                         'session_id': config.session,
                         'startup_seconds': round(time.monotonic() - started, 3)}), flush=True)
        while not supervisor.stopping:
            supervisor.check()
            time.sleep(0.2)
        return 0
    except InterruptedError:
        return 0
    finally:
        ready.unlink(missing_ok=True)
        supervisor.stop()
        passwd.unlink(missing_ok=True)
        for sig, old in old_handlers.items():
            signal.signal(sig, old)


def main():
    os.umask(0o077)
    try:
        config = Config.from_env(os.environ)
        action = sys.argv[1] if len(sys.argv) == 2 else ''
        if action == 'run':
            return run(config, dict(os.environ))
        if action == 'health':
            health(config, child_env(os.environ, config))
            return 0
        if action == 'open-target':
            target = json.loads((config.state / 'session.json').read_text()).get('CYBERPOD_TARGET_URL')
            if not target:
                raise ValueError('this Lab does not provide a target URL')
            os.execvpe('firefox-esr', ['firefox-esr', '--new-tab', http_url(target)],
                       child_env(os.environ, config))
        raise ValueError('expected run, health, or open-target')
    except Exception as exc:
        # Configuration errors are deliberately value-free; never print env or
        # child command input. OSError paths may identify a missing mount only.
        print(f'[cyberpod-kali] {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
