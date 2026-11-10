import asyncio
from types import SimpleNamespace

import pytest

from tools import research_login as login


@pytest.fixture
def session(tmp_path, monkeypatch):
    import config
    path = tmp_path / 'login.json'
    monkeypatch.setenv('MC_RESEARCH_LOGIN', str(path))
    monkeypatch.setattr(config, 'LOGIN_TYPE', 'qrcode')
    monkeypatch.setattr(config, 'PLATFORM', 'xhs')
    return path


async def until(path, phase):
    for _ in range(200):
        state = login.read(path)
        if state.get('phase') == phase:
            return state
        await asyncio.sleep(.01)
    raise AssertionError('Login phase never reached: ' + phase)


def test_timeout_refreshes_and_success_removes_qr(session, monkeypatch):
    monkeypatch.setattr(login, 'SCAN_SECONDS', .02)
    attempts = []
    reloads = []
    async def reload(**kwargs):
        reloads.append(True)
    @login.research_qr
    async def begin(self):
        attempts.append(True)
        login.publish_qr('data:image/png;base64,test')
        if len(attempts) == 1:
            await asyncio.sleep(10)
    asyncio.run(begin(SimpleNamespace(context_page=SimpleNamespace(reload=reload))))
    assert len(attempts) == 2 and len(reloads) == 1
    assert login.read(session)['phase'] == 'completed'
    assert 'image' not in login.read(session)


def test_skip_cancels_login_and_discards_image(session):
    cancelled = []
    @login.research_qr
    async def begin(self):
        login.publish_qr('data:image/png;base64,test')
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    async def check():
        task = asyncio.create_task(begin(SimpleNamespace()))
        state = await until(session, 'scanning')
        login.write(str(session) + '.action', {'id': state['id'], 'action': 'skip'})
        with pytest.raises(login.LoginSkipped):
            await task
    asyncio.run(check())
    assert cancelled and login.read(session)['phase'] == 'skipped'
    assert 'image' not in login.read(session)
    assert not session.with_name(session.name + '.action').exists()


def test_failed_login_waits_for_refresh_and_ignores_stale_skip(session):
    attempts = []
    async def reload(**kwargs):
        pass
    @login.research_qr
    async def begin(self):
        attempts.append(True)
        if len(attempts) == 1:
            raise SystemExit()
    async def check():
        task = asyncio.create_task(begin(SimpleNamespace(context_page=SimpleNamespace(reload=reload))))
        state = await until(session, 'failed')
        login.write(str(session) + '.action', {'id': 'old-QR', 'action': 'skip'})
        await asyncio.sleep(.05)
        assert not task.done()
        login.write(str(session) + '.action', {'id': state['id'], 'action': 'refresh'})
        await task
    asyncio.run(check())
    assert len(attempts) == 2 and login.read(session)['phase'] == 'completed'


@pytest.mark.parametrize('skip_all', [False, True])
def test_workflow_skips_login_and_continues_next_platform(tmp_path, monkeypatch, skip_all):
    from api.routers import research as routes
    from api.services.research_store import ResearchStore
    store = ResearchStore(tmp_path / 'home', tmp_path / 'data')
    worker = routes.Workflow(store)
    monkeypatch.setattr(routes, 'CAPTURE_FILE', tmp_path / 'settings.json')
    manager = routes.crawler_manager
    monkeypatch.setattr(manager, 'research_owner', 'test')
    monkeypatch.setattr(manager, 'process', None)
    monkeypatch.setattr(manager, 'status', 'idle')
    monkeypatch.setattr(manager, '_read_task', None)
    seen = []
    async def start(request, env):
        seen.append(request.platform.value)
        login.write(env['MC_RESEARCH_LOGIN'], {'phase': 'skipped' if skip_all or len(seen) == 1 else 'completed'})
        manager.process = SimpleNamespace(poll=lambda: 0)
        manager.status = 'idle'
        manager._read_task = asyncio.create_task(asyncio.sleep(0))
        return True
    monkeypatch.setattr(manager, 'start', start)
    run = store.create_run({'description': '测试跳过登录', 'name': '', 'platforms': ['xhs','dy'],
                            'keywords': {'xhs':['物品'], 'dy':['物品']}, 'config': routes.CaptureSettings().model_dump()})
    asyncio.run(worker.capture(run['id']))
    result = store.run(run['id'])
    assert seen == ['xhs', 'dy']
    assert {task['platform']:task['status'] for task in result['tasks']} == {'xhs':'skipped', 'dy':'skipped' if skip_all else 'completed'}
    assert result['status'] == ('stopped' if skip_all else 'partial') and result['counts']['failures'] == 0
    assert not worker.login_path.exists()


def test_login_api_rejects_stale_actions_and_only_exposes_active_session(session, monkeypatch):
    from api.routers import research as routes
    from fastapi import HTTPException
    monkeypatch.setattr(routes, 'workflow', lambda: SimpleNamespace(login_path=session))
    monkeypatch.setattr(routes.crawler_manager, 'research_owner', 'active')
    login.write(session, {'id':'new', 'phase':'scanning', 'image':'data:image/png;base64,test'})
    with pytest.raises(HTTPException) as exc:
        routes.login_action(routes.LoginAction(id='old', action='skip'))
    assert exc.value.status_code == 409
    routes.login_action(routes.LoginAction(id='new', action='refresh'))
    assert login.read(str(session)+'.action')['action'] == 'refresh'
    monkeypatch.setattr(routes.crawler_manager, 'research_owner', None)
    assert routes.login_status() is None


def test_cookie_login_keeps_existing_behavior(session, monkeypatch):
    import config
    monkeypatch.setattr(config, 'LOGIN_TYPE', 'cookie')
    @login.research_qr
    async def begin(self):
        raise ValueError('cookie rejected')
    with pytest.raises(ValueError, match='cookie rejected'):
        asyncio.run(begin(None))
    assert not session.exists()
