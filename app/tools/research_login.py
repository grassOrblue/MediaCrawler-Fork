"""Ephemeral QR login handoff between the crawler and the local research page."""
import asyncio
import functools
import json
import os
import time
import uuid
from pathlib import Path

SCAN_SECONDS = 300


class LoginSkipped(Exception):
    pass


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    temporary.replace(path)


def publish_qr(image):
    path = os.environ.get('MC_RESEARCH_LOGIN')
    if not path:
        return False
    state = read(path)
    if state.get('phase') != 'preparing':
        return True
    write(path, {**state, 'phase': 'scanning', 'image': image, 'expires_at': time.time() + SCAN_SECONDS})
    return True


def research_qr(function):
    @functools.wraps(function)
    async def wrapped(self, *args, **kwargs):
        import config
        path = os.environ.get('MC_RESEARCH_LOGIN')
        if not path or config.LOGIN_TYPE != 'qrcode':
            return await function(self, *args, **kwargs)
        action_path = path + '.action'
        attempt = 0
        try:
            while True:
                attempt += 1
                state = {'id': uuid.uuid4().hex, 'platform': config.PLATFORM, 'attempt': attempt, 'phase': 'preparing'}
                write(path, state)

                async def login():
                    try:
                        await function(self, *args, **kwargs)
                    except SystemExit as exc:
                        raise RuntimeError('扫码登录未完成') from exc

                task = asyncio.create_task(login())
                started = time.monotonic()
                action = ''
                try:
                    while not task.done():
                        command = read(action_path)
                        if command.get('id') == state['id']:
                            action = command.get('action')
                            break
                        if time.monotonic() - started >= SCAN_SECONDS:
                            action = 'refresh'
                            break
                        await asyncio.sleep(.5)
                    if action == 'skip':
                        write(path, {**state, 'phase': 'skipped'})
                        raise LoginSkipped('用户跳过此平台登录')
                    if not action:
                        await task
                        write(path, {**state, 'phase': 'completed'})
                        return
                except LoginSkipped:
                    raise
                except Exception:
                    # Acquisition failures wait for an explicit retry; scan timeouts refresh automatically.
                    write(path, {**state, 'phase': 'failed'})
                    while True:
                        command = read(action_path)
                        if command.get('id') == state['id'] and command.get('action') in ('skip', 'refresh'):
                            if command['action'] == 'skip':
                                write(path, {**state, 'phase': 'skipped'})
                                raise LoginSkipped('用户跳过此平台登录')
                            break
                        await asyncio.sleep(.5)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                write(path, {**state, 'phase': 'preparing'})
                try:
                    await self.context_page.reload(timeout=30000)
                except Exception:
                    pass
        finally:
            Path(action_path).unlink(missing_ok=True)
    return wrapped
