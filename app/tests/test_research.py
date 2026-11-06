"""Research workflow checks use isolated databases and fake platform/model responses."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from api.services.library_store import LibraryStore
from api.services.research_store import ResearchStore, title_key
from api.services.research_ai import ResearchAI, SettingsFile, AISettings, fingerprint, EXTRACT, MERGE
from api.routers.research import Workflow, RunRequest


@pytest.fixture
def store(tmp_path):
    result = ResearchStore(tmp_path / 'home', tmp_path / 'data')
    result.library.sqlite_source = None
    return result


def batch(store, platforms=('xhs',), **config):
    run = store.create_run({'name': '支架研究', 'description': '看看支架使用痛点', 'platforms': list(platforms),
        'keywords': {p: ['支架 吐槽', '支架 改进'] for p in platforms},
        'config': {'max_posts': 50, 'max_comments': 50, 'enable_comments': True, 'headless': False, 'request_interval': 5, **config}})
    store.set_run(run['id'], 'running')
    for p in platforms:
        store.set_platform(run['id'], p, 'running')
    return run['id']


def post(store, rid, pid, text='底座太轻', platform='xhs', title='支架'):
    assert store.candidate(rid, platform, pid, '支架', 10000)
    return store.save_item(rid, platform, {'note_id': pid, 'title': title, 'desc': text, 'note_url': 'https://example.invalid/' + pid})


def ai(store, tmp_path):
    settings = SettingsFile(tmp_path / 'settings.json')
    settings.save(AISettings(api_key='secret-for-tests', model='test-model', context_tokens=32768, max_output_tokens=1024))
    return ResearchAI(store, settings)


async def finish(engine, job):
    engine.confirm(job['id'])
    await engine.tasks[job['id']]
    return engine.store.job(job['id'])


async def fake_model(cfg, prompt):
    data = json.loads(prompt[1]['content'])
    instruction = prompt[0]['content']
    if '分析研究描述' in instruction:
        result = {'understanding': '调查支架痛点', 'product': '支架', 'audience': '使用者', 'scenes': ['桌面'], 'questions': [],
                  'keywords': {p: [{'text': '支架 吐槽', 'reason': '发现问题'}] for p in data['platforms']}}
    elif EXTRACT in instruction:
        result = {'items': []}
        for item in data['materials']:
            name = '太重' if '太重' in item['text'] else '太轻'
            result['items'].append({'name': name, 'problem': name, 'scene': '桌面', 'explicit': '', 'suggestion': '调整底座',
                                    'evidence': [{'id': item['id'], 'quote': item['text']}]})
    elif MERGE in instruction:
        groups = {}
        for item in data:
            group = groups.setdefault(item['name'], {k: item[k] for k in ('name', 'problem', 'scene', 'explicit', 'suggestion')} | {'members': []})
            group['members'].append(item['id'])
        result = {'groups': list(groups.values())}
    else:
        result = {'summary': '底座重量是本批样本的主要痛点，建议进一步验证。'}
    return result, {'prompt_tokens': 30, 'completion_tokens': 20, 'total_tokens': 50}


def test_persistent_dedup_counts_and_batch_filters(store):
    rid = batch(store, max_posts=2, max_comments=1)
    post(store, rid, '1')
    post(store, rid, '2', title='支架')
    assert not store.candidate(rid, 'xhs', '1', '第二个关键词', 2)
    assert not store.candidate(rid, 'xhs', '3', '支架', 2)
    row = {'note_id': '1', 'comment_id': 'c1', 'content': '要是有配重就好了'}
    assert store.save_item(rid, 'xhs', row, True)
    assert not store.save_item(rid, 'xhs', row, True)
    assert not store.save_item(rid, 'xhs', row | {'comment_id': 'c2'}, True)
    counts = store.run(rid)['counts']
    assert (counts['new_posts'], counts['checked'], counts['new_comments'], counts['duplicate_comments']) == (2, 2, 1, 1)
    store.set_run(rid, 'completed')
    second = batch(store)
    assert not store.candidate(second, 'xhs', '1', '支架', 50)
    assert not store.save_item(second, 'xhs', row | {'comment_id': 'c3'}, True)
    assert store.run(second)['counts']['old_posts'] == 1
    assert store.documents(second) == []
    assert store.library.list_posts({'run_id': rid})['total'] == 2
    assert store.library.list_posts({'run_id': second})['total'] == 1
    assert title_key('Ａ B \n') == title_key('ab')
    assert not store.candidate(second, 'xhs', '', '支架', 50)
    assert store.run(second)['counts']['failures'] == 1


def test_migration_backup_annotations_and_recovery(tmp_path):
    home = tmp_path / 'legacy'
    legacy = LibraryStore(tmp_path / 'data', home)
    with legacy.connect() as db:
        legacy.upsert(db, 'xhs', {'note_id': 'old', 'title': '旧内容', 'desc': '正文'})
    legacy.annotate('xhs', 'old', {'saved': True, 'note': '不能丢失'})
    migrated = ResearchStore(home, tmp_path / 'data')
    assert len(list(home.glob('library.before-research-*.sqlite3'))) == 1
    assert migrated.library.detail('xhs', 'old')['note'] == '不能丢失'
    rid = batch(migrated)
    assert not migrated.candidate(rid, 'xhs', 'old', '关键词', 50)
    pending = migrated.create_job('keywords', {}, 'hash', {}, state={})
    migrated.claim_job(pending['id'], 'hash')
    migrated.recover()
    assert ResearchStore(home, tmp_path / 'data').run(rid)['status'] == 'interrupted'
    assert migrated.job(pending['id'])['status'] == 'interrupted'
    assert len(list(home.glob('library.before-research-*.sqlite3'))) == 1
    assert migrated.library.detail('xhs', 'old')['saved'] == 1


def test_local_estimates_no_network_and_stale_approval(store, tmp_path):
    engine = ai(store, tmp_path)
    engine.request = AsyncMock(side_effect=AssertionError('estimate must not contact provider'))
    job = engine.estimate_keywords('看看支架使用痛点', ['xhs'])
    assert job['budget']['input_tokens'] > 0
    assert 'secret-for-tests' not in json.dumps(engine.settings.public())
    cfg = engine.settings.load()
    cfg.model = 'changed'
    engine.settings.save(cfg)
    async def check():
        with pytest.raises(ValueError, match='预算已失效'):
            engine.confirm(job['id'])
    asyncio.run(check())
    engine.request.assert_not_called()


def test_counting_original_evidence_and_frozen_material(store, tmp_path):
    rid = batch(store)
    post(store, rid, '1', '底座太轻', title='底座太轻')
    post(store, rid, '2', '底座太轻', title='底座太轻')
    post(store, rid, '3', '底座太重', title='底座太重')
    store.save_item(rid, 'xhs', {'note_id': '1', 'comment_id': 'c1', 'content': '底座太轻'}, True)
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    engine.request = AsyncMock(side_effect=fake_model)
    job = engine.estimate_analysis(rid)
    assert len(job['payload']['segments']) == 2
    assert len(job['payload']['documents']) == 4
    assert all('author' not in doc for doc in job['payload']['documents'])
    with store.connect() as db:
        db.execute("UPDATE posts SET body='后来改了' WHERE post_id='1'")
    result = asyncio.run(finish(engine, job))
    assert result['status'] == 'completed', result['error']
    demands = {d['name']: d for d in result['result']['demands']}
    assert demands['太轻']['mentions'] == 3
    assert demands['太轻']['posts'] == 2
    assert demands['太重']['mentions'] == 1
    assert result['result']['usage']['total_tokens'] > 0
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM research_evidence WHERE job_id=?', (job['id'],)).fetchone()[0] == 4


def test_duplicate_confirmation_and_empty_report(store, tmp_path):
    rid = batch(store)
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    engine.request = AsyncMock(side_effect=AssertionError('empty report needs no requests'))
    job = engine.estimate_analysis(rid)
    async def check():
        engine.confirm(job['id'])
        with pytest.raises(ValueError):
            engine.confirm(job['id'])
        await engine.tasks[job['id']]
    asyncio.run(check())
    result = store.job(job['id'])
    assert result['status'] == 'completed'
    assert result['result']['demands'] == []
    engine.request.assert_not_called()


def test_budget_pause_reestimate_and_invalid_evidence(store, tmp_path):
    rid = batch(store)
    post(store, rid, '1')
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    engine.request = AsyncMock(side_effect=fake_model)
    job = engine.estimate_analysis(rid)
    store.update_job(job['id'], budget=job['budget'] | {'limit_tokens': 1})
    paused = asyncio.run(finish(engine, job))
    assert paused['status'] == 'needs_budget'
    engine.request.assert_not_called()
    continued = engine.reestimate(job['id'])
    assert asyncio.run(finish(engine, continued))['status'] == 'completed'
    async def bad(cfg, prompt):
        data = json.loads(prompt[1]['content'])
        return {'items': [{'name': '虚构', 'problem': '', 'scene': '', 'explicit': '', 'suggestion': '',
                           'evidence': [{'id': data['materials'][0]['id'], 'quote': '不在原文里'}]}]}, {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
    engine.request = bad
    invalid = asyncio.run(finish(engine, engine.estimate_analysis(rid)))
    assert invalid['status'] == 'failed'
    assert invalid['state']['usage']['total_tokens'] == 2
    assert invalid['result'] == {}


def test_timeout_not_retried_and_usage_on_invalid_json(store, tmp_path):
    engine = ai(store, tmp_path)
    engine.request = AsyncMock(side_effect=ValueError('请求超时，可能已计费'))
    result = asyncio.run(finish(engine, engine.estimate_keywords('看看支架使用痛点', ['xhs'])))
    assert result['status'] == 'failed'
    assert result['state']['pending_request']
    assert engine.request.call_count == 1
    engine.request = AsyncMock(return_value=('not-json', {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}))
    result = asyncio.run(finish(engine, engine.estimate_keywords('看看支架使用痛点', ['xhs'])))
    assert result['status'] == 'failed'
    assert result['state']['usage']['total_tokens'] == 7


def test_provider_http_adapters_and_secret_safe_errors(store, tmp_path, monkeypatch):
    engine = ai(store, tmp_path)
    real_client = httpx.AsyncClient
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 1}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    for provider in ('deepseek', 'kimi', 'doubao', 'custom'):
        cfg = engine.settings.load().model_copy(update={'provider': provider})
        text, usage = asyncio.run(engine.request(cfg, [{'role': 'user', 'content': 'test'}]))
        assert json.loads(text) == {'ok': True}
        assert ('max_completion_tokens' if provider == 'doubao' else 'max_tokens') in captured[-1]
    with pytest.raises(ValueError):
        AISettings(base_url='http://remote.invalid')


@pytest.mark.parametrize('platform', ['xhs', 'dy', 'bili'])
def test_three_platforms_early_skip_and_shared_limit(store, monkeypatch, platform):
    import config
    from tools import research_capture as capture
    rid = batch(store, [platform], max_posts=2)
    with store.connect() as db:
        store.library.upsert(db, platform, {'note_id': 'old', 'title': '旧帖', 'desc': '旧正文'})
    monkeypatch.setenv('MC_RESEARCH_ID', rid)
    monkeypatch.setattr(capture, 'context', lambda: (store, rid))
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    async def search(**kwargs):
        if platform == 'xhs':
            return {'items': [{'id': p} for p in ['old', 'new', 'too-many']], 'has_more': False}
        if platform == 'dy':
            return {'data': [{'aweme_info': {'aweme_id': p, 'desc': '支架太轻', 'author': {}, 'statistics': {}}} for p in ['old', 'new', 'too-many']], 'has_more': 0}
        return {'result': [{'aid': p} for p in ['old', 'new', 'too-many']]}
    async def detail(**kwargs):
        pid = kwargs.get('note_id') or kwargs.get('aid')
        assert pid == 'new'
        if platform == 'xhs':
            return {'note_id': pid, 'title': '支架', 'desc': '底座太轻', 'user': {}}
        return {'View': {'aid': pid, 'title': '支架', 'desc': '很长的简介' * 200, 'owner': {}, 'stat': {}}}
    comments = AsyncMock()
    crawler = SimpleNamespace(xhs_client=SimpleNamespace(get_note_by_keyword=search),
        dy_client=SimpleNamespace(search_info_by_keyword=search), bili_client=SimpleNamespace(search_video_by_keyword=search),
        get_note_detail_async_task=AsyncMock(side_effect=detail), get_video_info_task=AsyncMock(side_effect=detail),
        batch_get_note_comments=comments, batch_get_video_comments=comments)
    asyncio.run(capture.search(crawler, platform))
    result = store.run(rid)
    assert result['counts']['checked'] == 2
    assert result['counts']['old_posts'] == 1
    assert result['counts']['new_posts'] == 1
    assert comments.call_count == 1
    assert comments.call_args.args[0] == ['new']
    if platform == 'bili':
        assert len(store.library.detail(platform, 'new')['body']) > 500
    if platform == 'dy':
        assert store.library.detail(platform, 'new')['original_url'].endswith('/new')


def test_workflow_sequential_platforms_partial_and_stop(store, tmp_path, monkeypatch):
    from api.routers import research as module
    manager = module.crawler_manager
    worker = Workflow(store)
    worker.ai = ai(store, tmp_path)
    monkeypatch.setattr(store.library, 'sync', lambda: None)
    monkeypatch.setattr(manager, 'research_owner', None)
    monkeypatch.setattr(manager, 'process', None)
    keyword = asyncio.run(finish_with_fake(worker.ai))
    request = RunRequest(description='看看支架使用痛点', keyword_job_id=keyword['id'], platforms=['xhs','dy'], keywords={'xhs':['支架'],'dy':['支架']})
    order = []
    async def start(config, env):
        order.append(config.platform.value)
        code = 1 if config.platform.value == 'dy' else 0
        manager.process = SimpleNamespace(poll=lambda: code)
        manager.status = 'error' if code else 'idle'
        manager._read_task = asyncio.create_task(asyncio.sleep(0))
        return True
    monkeypatch.setattr(manager, 'start', start)
    async def check():
        run = await worker.launch(request)
        await worker.task
        assert worker.store.run(run['id'])['status'] == 'partial'
        assert order == ['xhs','dy']
        assert manager.research_owner is None
        async def long_start(config, env):
            manager.process = SimpleNamespace(poll=lambda: None)
            manager._read_task = asyncio.create_task(asyncio.sleep(100))
            return True
        async def stop():
            manager.process = None
            return True
        monkeypatch.setattr(manager, 'start', long_start)
        monkeypatch.setattr(manager, 'stop', stop)
        run = await worker.launch(request)
        await asyncio.sleep(0)
        stopped = await worker.stop(run['id'])
        assert stopped['status'] == 'stopped'
        assert manager.research_owner is None
    asyncio.run(check())


async def finish_with_fake(engine):
    engine.request = fake_model
    return await finish(engine, engine.estimate_keywords('看看支架使用痛点', ['xhs','dy']))


def test_api_confirmation_export_and_clarification(store, tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from api.routers import research as module
    worker = Workflow(store)
    worker.ai = ai(store, tmp_path)
    worker.ai.request = AsyncMock(side_effect=fake_model)
    monkeypatch.setattr(module, 'workflow', lambda: worker)
    app = FastAPI()
    app.include_router(module.router, prefix='/api')
    @app.exception_handler(ValueError)
    async def invalid(request: Request, exc: ValueError):
        return JSONResponse({'detail': str(exc)}, status_code=400)
    rid = batch(store)
    post(store, rid, '1', '底座太轻')
    store.set_run(rid, 'completed')
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            settings = (await client.get('/api/research/settings')).json()
            assert settings['settings']['has_key'] and 'api_key' not in settings['settings']
            response = await client.post('/api/research/runs/' + rid + '/estimate')
            assert response.status_code == 200
            job = response.json()
            worker.ai.request.assert_not_called()
            response = await client.post('/api/research/jobs/' + job['id'] + '/confirm')
            assert response.status_code == 200
            assert (await client.post('/api/research/jobs/' + job['id'] + '/confirm')).status_code == 400
            await worker.ai.tasks[job['id']]
            report = (await client.get('/api/research/jobs/' + job['id'])).json()
            assert report['status'] == 'completed'
            assert not set(report).intersection({'payload', 'state', 'config_hash'})
            exported = await client.get('/api/research/jobs/' + job['id'] + '/export')
            assert rid in exported.text and '底座太轻' in exported.text
            contents = (await client.get('/api/research/runs/' + rid + '/contents')).json()
            assert len(contents['items']) == 1
            detail = (await client.get('/api/research/runs/' + rid)).json()
            assert detail['jobs'][0]['result'] == {}  # history polling does not load all old report bodies
            assert (await client.post('/api/research/runs', json={'description': '改了目标', 'platforms': ['xhs'], 'keyword_job_id': job['id'], 'keywords': {'xhs': ['词']}})).status_code == 400
            assert (await client.post('/api/research/runs', json={'description': '支架', 'platforms': ['xhs'], 'keyword_job_id': 'x', 'keywords': {'xhs': ['词,分隔']}})).status_code == 422
    asyncio.run(check())


def test_long_text_no_truncation_and_reconciliation(store, tmp_path):
    rid = batch(store)
    text = '需要一个更稳的底座。' * 1200
    post(store, rid, '1', text, title=text)
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    material = engine.estimate_analysis(rid)['payload']
    assert ''.join(s['text'] for s in material['segments']) == text
    assert len(material['segments']) > 1
    engine.request = fake_model
    cfg = engine.configured()
    job = engine.store.create_job('analysis', {}, fingerprint(cfg), {'limit_tokens': 10000000})
    groups = []
    for i in range(3):
        groups.append({'name': '太轻' if i < 2 else '太重', 'problem': '', 'scene': '', 'explicit': '', 'suggestion': '',
                       'evidence': [{'id': str(i), 'platform': 'xhs', 'post_id': str(i)}]})
    merged = asyncio.run(engine.reconcile(job, cfg, {}, groups))
    assert len(merged) == 2
    assert sorted(len(g['evidence']) for g in merged) == [1, 2]


def test_timestamp_batch_ids_all_batches_and_legacy_filter(store, monkeypatch):
    from api.services import research_store as module
    stamp = int(datetime(2026, 11, 5, 16, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(module, 'now', lambda: stamp)
    def create(name=''):
        return store.create_run({'name': name, 'description': '探索物品烦恼', 'keywords': {'xhs': ['使用烦恼']},
                                'platforms': ['xhs'], 'config': {'max_posts': 50, 'max_comments': 50}})
    first = create()
    second = create('我的研究')
    assert first['id'] == '20261106-000000'
    assert first['name'] == first['id']
    assert second['id'] == first['id'] + '-02'
    assert second['name'] == '我的研究'
    with store.connect() as db:
        store.library.upsert(db, 'xhs', {'note_id': 'historic', 'title': '历史未分批'})
    store.set_platform(first['id'], 'xhs', 'running')
    assert not store.candidate(first['id'], 'xhs', 'historic', '词', 50)
    post(store, first['id'], 'new')
    assert store.library.list_posts({'run_id': '__legacy__'})['total'] == 1
    assert store.library.list_posts({'run_id': first['id']})['total'] == 2
    for index in range(101):
        create()
    options = store.library.batches()
    assert len(options['items']) == 103  # selector includes batches beyond research history's 100-row limit
    assert options['latest_id'] == '20261106-000000-103'
    assert options['items'][-1]['id'] == first['id']


def test_open_discovery_accepts_no_specific_product(store, tmp_path):
    engine = ai(store, tmp_path)
    description = '我还不知道具体产品，想看看大家最近在吐槽哪些日常物品'
    async def discovery(cfg, prompt):
        assert json.loads(prompt[1]['content'])['description'] == description
        assert '未指定产品是有效目标' in prompt[0]['content']
        return {'understanding': '发现不同日常物品的具体使用痛点', 'product': '不限品类（探索发现）', 'audience': '日常用品使用者',
                'scenes': [], 'questions': [], 'keywords': {'xhs': [
                    {'text': '买了最后悔的东西', 'reason': '发现物品及实际问题'},
                    {'text': '用起来最麻烦的东西', 'reason': '了解使用烦恼'},
                    {'text': '这个设计不合理', 'reason': '发现改进诉求'}]}}, {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}
    engine.request = discovery
    result = asyncio.run(finish(engine, engine.estimate_keywords(description, ['xhs'])))
    assert result['status'] == 'completed'
    assert result['result']['questions'] == []
    assert len(result['result']['keywords']['xhs']) == 3


def test_library_batch_options_before_research_upgrade(tmp_path):
    library = LibraryStore(tmp_path / 'data', tmp_path / 'home')
    with library.connect() as db:
        library.upsert(db, 'xhs', {'note_id': 'old', 'title': '旧数据'})
    assert library.batches() == {'items': [], 'latest_id': ''}
    assert library.list_posts({'run_id': '__legacy__'})['total'] == 1
