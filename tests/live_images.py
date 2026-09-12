"""Opt-in Docker image acceptance, run on a disposable Linux CI worker."""
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from uuid import uuid4


async def check_gateway(session_id, desktop_ip, password):
    from aiohttp import web
    from playwright.async_api import async_playwright, expect
    from cyberpod_core.hardening import apply_hardening
    from cyberpod_gateway.app import DesktopGateway, create_app

    # Model the trusted worker-side tunnel with one fixed container endpoint.
    # The gateway only accepts loopback upstreams.
    connections = set()
    async def forward(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        remote_writer = None
        jobs = []
        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
        try:
            remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(desktop_ip, 6080), 5)
            jobs = [asyncio.create_task(copy(reader, remote_writer)), asyncio.create_task(copy(remote_reader, writer))]
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for stream in (writer, remote_writer):
                if stream:
                    stream.close()
            connections.discard(task)
    tunnel = await asyncio.start_server(forward, '127.0.0.1', 0)
    port = tunnel.sockets[0].getsockname()[1]
    gateway = DesktopGateway(public_base='')
    gateway.register(session_id, f'http://127.0.0.1:{port}', password)
    app = create_app(gateway, operator_token=secrets.token_urlsafe(32), subject_for_request=lambda _: 'image-smoke')
    apply_hardening(app, require_https=False)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        address = runner.addresses[0]
        grant, _ = gateway.issue_url(session_id, 'image-smoke', 1)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{address[1]}' + grant)
                await expect(page.get_by_role('status')).to_have_text('Connected', timeout=30000)
                assert not errors, errors
                gateway.revoke(session_id)
                await expect(page.get_by_role('status')).to_have_text('Disconnected. Reconnect to continue.', timeout=10000)
            finally:
                await browser.close()
    finally:
        await runner.cleanup()
        tunnel.close()
        await tunnel.wait_closed()
        for connection in list(connections):
            connection.cancel()
        await asyncio.gather(*list(connections), return_exceptions=True)
    print('PASS: Chrome connects to actual Kali through the authenticated gateway; revocation disconnects it')


def run(*args, check=True, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


def health(name, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = run('docker', 'inspect', '--format', '{{.State.Health.Status}}', name).stdout.strip()
        if value == 'healthy':
            return
        if value == 'unhealthy':
            break
        time.sleep(2)
    logs = run('docker', 'logs', '--tail', '60', name, check=False).stdout
    raise RuntimeError(f'{name} failed readiness: {logs}')


def main():
    names = []
    networks = []
    prefix = 'cp-smoke-' + uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        directory.chmod(0o755)
        try:
            for attempt in ('a', 'b'):
                network = prefix + '-' + attempt
                run('docker', 'network', 'create', '--internal', network)
                networks.append(network)
            session_id = str(uuid4())
            config = {'session_id': session_id, 'generation': 1, 'username': 'admin',
                      'password': secrets.token_urlsafe(12), 'flag': 'CYBERPOD{' + secrets.token_hex(16) + '}',
                      'expires_at': time.time() + 600}
            config_path = directory / 'target.json'
            config_path.write_text(json.dumps(config)); config_path.chmod(0o444)
            for attempt, network in zip(('a', 'b'), networks):
                name = prefix + '-target-' + attempt
                names.append(name)
                run('docker', 'run', '-d', '--name', name, '--network', network, '--network-alias', 'target',
                    '--user', '1000:1000', '--read-only', '--cap-drop=ALL', '--security-opt', 'no-new-privileges',
                    '--memory', '256m', '--pids-limit', '64', '--mount', f'type=bind,src={config_path},dst=/run/secrets/target.json,readonly',
                    'cyberpod/hydra-target:dev')
                health(name)
            desktop = prefix + '-desktop'
            names.append(desktop)
            vnc_password = secrets.token_urlsafe(18)
            run('docker', 'run', '-d', '--name', desktop, '--network', networks[0], '--user', '1000:1000',
                '--read-only', '--cap-drop=ALL', '--security-opt', 'no-new-privileges', '--memory', '2g', '--pids-limit', '256',
                '--shm-size', '128m', '--tmpfs', '/home/student:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700',
                '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
                '-e', f'CYBERPOD_SESSION_ID={session_id}', '-e', 'CYBERPOD_LAB_ID=smoke',
                '-e', 'CYBERPOD_TARGET_URL=http://target:8080/login',
                '-e', 'CYBERPOD_VNC_PASSWORD=' + vnc_password, 'cyberpod/kali-desktop:dev')
            health(desktop)
            auth = base64.b64encode(('admin:' + config['password']).encode()).decode()
            probe = "import urllib.request; r=urllib.request.Request('http://target:8080/vault',headers={'Authorization':'Basic " + auth + "'}); assert urllib.request.urlopen(r,timeout=3).status==200"
            run('docker', 'exec', desktop, 'python3', '-c', probe)
            other_ip = run('docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', names[1]).stdout.strip()
            isolated = "import socket; s=socket.socket(); s.settimeout(2); assert s.connect_ex(('" + other_ip + "',8080)) != 0"
            run('docker', 'exec', desktop, 'python3', '-c', isolated)
            run('docker', 'exec', desktop, 'sh', '-c', 'command -v hydra && command -v nmap && command -v firefox-esr')
            print('PASS: actual XFCE/VNC/noVNC health, training target login, and cross-network connection rejection')
            desktop_ip = run('docker', 'inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', desktop).stdout.strip()
            asyncio.run(check_gateway(session_id, desktop_ip, vnc_password))
        finally:
            for name in reversed(names):
                run('docker', 'rm', '-f', name, check=False)
            for network in reversed(networks):
                run('docker', 'network', 'rm', network, check=False)
            remaining = run('docker', 'ps', '-aq', '--filter', 'name=' + prefix).stdout.strip()
            assert not remaining, 'Smoke containers were not cleaned'
            print('PASS: smoke containers cleaned')

if __name__ == '__main__':
    main()
