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
    monkeypatch.setattr(config, 'SAVE_DATA_OPTION', 'sqlite')
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
        batch_get_note_comments=comments, batch_get_video_comments=comments, download_media=AsyncMock())
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


def test_output_truncation_retries_only_smaller_unfinished_chunk(store, tmp_path):
    rid = batch(store)
    for i in range(8):
        post(store, rid, str(i), '底座太轻 ' + str(i))
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    job = engine.estimate_analysis(rid)
    assert len(job['payload']['chunks']) == 2
    calls = []
    async def truncating(cfg, prompt):
        calls.append(prompt)
        if len(calls) == 2:
            return {'_response_error': '达到输出上限', '_finish_reason': 'length'}, {'prompt_tokens': 10, 'completion_tokens': 1024, 'total_tokens': 1034}
        return await fake_model(cfg, prompt)
    engine.request = truncating
    failed = asyncio.run(finish(engine, job))
    assert failed['status'] == 'failed'
    assert failed['state']['chunk_index'] == 1
    assert len(failed['state']['candidates']) == 4
    assert len(calls) == 2
    retry = engine.reestimate(failed['id'])
    assert retry['status'] == 'estimated'
    assert len(calls) == 2  # estimating never performs a retry
    assert len(retry['payload']['chunks']) == 3
    assert retry['payload']['chunks'][0] == job['payload']['chunks'][0]
    assert [len(c) for c in retry['payload']['chunks']] == [4, 2, 2]
    assert retry['state']['chunk_index'] == 1
    assert len(store.job(failed['id'])['payload']['chunks']) == 2
    completed_ids = {s['id'] for s in job['payload']['chunks'][0]}
    async def continuing(cfg, prompt):
        if EXTRACT in prompt[0]['content']:
            assert not completed_ids.intersection(s['id'] for s in json.loads(prompt[1]['content'])['materials'])
        return await fake_model(cfg, prompt)
    engine.request = continuing
    result = asyncio.run(finish(engine, retry))
    assert result['status'] == 'completed', result['error']
    assert result['result']['demands'][0]['mentions'] == 8


def test_split_single_segment_preserves_sources_and_short_chunk_quote_check(store, tmp_path):
    rid = batch(store)
    post(store, rid, '1', '甲' * 150 + '乙' * 150, title='')
    store.set_run(rid, 'completed')
    engine = ai(store, tmp_path)
    job = engine.estimate_analysis(rid)
    store.update_job(job['id'], status='failed', error='length', state={'stage': 'extract', 'finish_reason': 'length'})
    retry = engine.reestimate(job['id'])
    parts = retry['payload']['chunks']
    assert len(parts) == 2
    assert ''.join(c[0]['text'] for c in parts) == '甲' * 150 + '乙' * 150
    assert all(next(s for s in retry['payload']['segments'] if s['id'] == c[0]['id'])['sources'] == ['e1'] for c in parts)
    bad = {'items': [{'name': '错误证据', 'problem': '', 'scene': '', 'explicit': '', 'suggestion': '', 'evidence': [{'id': parts[0][0]['id'], 'quote': '乙'}]}]}
    with pytest.raises(ValueError, match='非原文片段'):
        engine.extracted(bad, parts[0], retry['payload'])


def test_compact_merge_preserves_fields_and_validates_members(store, tmp_path):
    engine = ai(store, tmp_path)
    cfg = engine.configured()
    job = store.create_job('analysis', {}, fingerprint(cfg), {'limit_tokens': 100000})
    groups = [{'name': '支架不稳', 'problem': '容易倒', 'scene': '桌面', 'explicit': '', 'suggestion': '加重', 'evidence': [{'id': '1'}]},
              {'name': '支架太轻', 'problem': '底座太轻', 'scene': '桌面', 'explicit': '增加配重', 'suggestion': '', 'evidence': [{'id': '2'}]}]
    engine.request = AsyncMock(return_value=({'groups': [{'members': ['0', '1']}]}, {}))
    merged = asyncio.run(engine.merge(job, cfg, {}, groups))
    assert len(merged[0]['evidence']) == 2
    assert merged[0]['explicit'] == '增加配重'
    assert '容易倒' in merged[0]['problem'] and '底座太轻' in merged[0]['problem']
    engine.request = AsyncMock(return_value=({'groups': [{'members': ['0', '0']}]}, {}))
    state = {}
    restored = asyncio.run(engine.merge(job, cfg, state, groups))
    assert len(restored) == 2
    assert {e['id'] for g in restored for e in g['evidence']} == {'1', '2'}
    assert state['last_merge_check']['within_group_duplicates'] == 1
    assert state['last_merge_check']['missing_candidates'] == 1
    assert state['grouping_warnings']


@pytest.mark.parametrize('reason,expected', [('length', '输出上限 1024'), ('content_filter', '审核拦截'), ('aborted', '中断')])
def test_finish_reason_is_specific_and_usage_kept(store, tmp_path, monkeypatch, reason, expected):
    engine = ai(store, tmp_path)
    real_client = httpx.AsyncClient
    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': 'partial'}, 'finish_reason': reason}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 1024, 'total_tokens': 1034}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)))
    result = asyncio.run(finish(engine, engine.estimate_keywords('支架吐槽', ['xhs'])))
    assert result['status'] == 'failed'
    assert expected in result['error']
    assert result['state']['finish_reason'] == reason
    assert result['state']['usage']['total_tokens'] == 1034
    assert result['state']['calls'] == 1


@pytest.mark.parametrize('response,expected_groups,conflicts,missing,invalid', [
    ({'groups': [{'members': [0, 1]}, {'members': [2, 3]}]}, [{0, 1}, {2, 3}], 0, 0, 0),
    ({'groups': [{'members': ['0', 0, '1']}, {'members': ['2', '3']}]}, [{0, 1}, {2, 3}], 0, 0, 0),
    ({'groups': [{'members': [0, 1]}, {'members': [1, 2]}, {'members': [3]}]}, [{0}, {2}, {3}, {1}], 1, 0, 0),
    ({'groups': [{'members': ['0', '1', '999', True, 1.0, None]}, {'members': ['3']}]}, [{0, 1}, {3}, {2}], 0, 1, 4),
    ({'groups': []}, [{0}, {1}, {2}, {3}], 0, 4, 0),
    ({'groups': [None, {'members': '0'}]}, [{0}, {1}, {2}, {3}], 0, 4, 2),
])
def test_group_membership_repairs_are_complete_and_conservative(store, tmp_path, response, expected_groups, conflicts, missing, invalid):
    engine = ai(store, tmp_path)
    cfg = engine.configured()
    job = store.create_job('analysis', {}, fingerprint(cfg), {'limit_tokens': 100000})
    candidates = [{'name': f'物品{i}的问题', 'problem': str(i), 'scene': '', 'explicit': '', 'suggestion': '',
                   'evidence': [{'id': str(i)}], '_lineage': [str(i)]} for i in range(4)]
    engine.request = AsyncMock(return_value=(response, {}))
    state = {}
    merged = asyncio.run(engine.merge(job, cfg, state, candidates))
    assert [{int(e['id']) for e in g['evidence']} for g in merged] == expected_groups
    assert sum(len(g['evidence']) for g in merged) == 4
    assert state['last_merge_check']['cross_group_conflicts'] == conflicts
    assert state['last_merge_check']['missing_candidates'] == missing
    assert state['last_merge_check']['invalid_entries'] == invalid
    if conflicts or missing or invalid:
        assert state['grouping_warnings']
    assert sorted(origin for g in merged for origin in g['_lineage']) == ['0', '1', '2', '3']


def test_retry_failed_global_merge_retains_all_extracted_and_local_results(store, tmp_path, monkeypatch):
    from api.routers import research as routes
    from fastapi import FastAPI
    engine = ai(store, tmp_path)
    rid = batch(store)
    for i in range(4):
        post(store, rid, str(i), '底座太轻 ' + str(i))
    store.set_run(rid, 'completed')
    job = engine.estimate_analysis(rid)
    payload = job['payload']
    candidates = []
    for chunk in payload['chunks']:
        raw = asyncio.run(fake_model(engine.configured(), [{'content': EXTRACT}, {'content': json.dumps({'materials': chunk})}]))[0]
        candidates.extend(engine.extracted(raw, chunk, payload))
    state = {'chunk_index': len(payload['chunks']), 'merge_index': 4, 'stage': 'merge', 'merge_size': 4,
             'candidates': candidates, 'batch_groups': candidates}
    store.update_job(job['id'], status='failed', error='需求归类遗漏、重复或虚构了候选编号', state=state)
    called_stages = []
    async def response(cfg, prompt):
        assert EXTRACT not in prompt[0]['content']
        called_stages.append('merge' if MERGE in prompt[0]['content'] else 'summary')
        if MERGE in prompt[0]['content']:
            return {'groups': [{'members': [0, 1]}, {'members': [1, 2]}]}, {}
        return {'summary': '测试摘要'}, {}
    engine.request = response
    retry = engine.reestimate(job['id'])
    assert not called_stages
    assert retry['state']['candidates'] == candidates
    assert retry['state']['batch_groups'] == candidates
    result = asyncio.run(finish(engine, retry))
    assert result['status'] == 'completed', result['error']
    assert called_stages == ['merge', 'summary']
    assert result['result']['warnings']
    assert {e['id'] for demand in result['result']['demands'] for e in demand['evidence']} == {d['id'] for d in payload['documents']}
    worker = SimpleNamespace(store=store)
    monkeypatch.setattr(routes, 'workflow', lambda: worker)
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    async def export():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            exported = await client.get('/api/research/jobs/' + result['id'] + '/export')
            assert exported.status_code == 200
            assert '归类说明' in exported.text and '冲突' in exported.text
    asyncio.run(export())


def test_unified_manual_modes_global_settings_and_cookie_privacy(store, tmp_path, monkeypatch):
    from api.routers import research as routes
    monkeypatch.setattr(routes, 'CAPTURE_FILE', tmp_path / 'capture.json')
    manager = routes.crawler_manager
    monkeypatch.setattr(manager, 'research_owner', None)
    monkeypatch.setattr(manager, 'process', None)
    preferences = routes.CapturePreferences(login_type='cookie', cookies={'xhs': 'test-cookie-secret'}, max_posts=77,
                                           enable_media=True, enable_sub_comments=True, save_option='jsonl')
    public = routes.save_capture_settings(preferences)
    assert public['has_cookies']['xhs']
    assert 'test-cookie-secret' not in json.dumps(public)
    routes.save_capture_settings(routes.CapturePreferences(login_type='cookie', max_posts=77, enable_media=True,
                                                          enable_sub_comments=True, save_option='jsonl'))
    assert routes.capture_preferences().cookies['xhs'] == 'test-cookie-secret'
    worker = Workflow(store)
    monkeypatch.setattr(store.library, 'sync', lambda: None)
    captured = []
    async def start(request, env):
        captured.append(request)
        manager.process = SimpleNamespace(poll=lambda: 0)
        manager.status = 'idle'
        manager._read_task = asyncio.create_task(asyncio.sleep(0))
        return True
    monkeypatch.setattr(manager, 'start', start)
    async def check():
        for mode, targets, expected in [('search', [], 'search'), ('auto', ['https://www.xiaohongshu.com/explore/abcdef'], 'detail'),
                                        ('auto', ['https://www.xiaohongshu.com/user/profile/abcdef'], 'creator')]:
            body = RunRequest(platforms=['xhs'], crawler_type=mode, keywords={'xhs': ['物品 吐槽']}, targets={'xhs': targets})
            run = await worker.launch(body)
            await worker.task
            request = captured[-1]
            assert request.crawler_type.value == expected
            assert request.cookies == 'test-cookie-secret'
            assert request.max_notes_count == 77 and request.enable_media and request.enable_sub_comments
            assert request.save_option.value == 'jsonl'
            assert 'test-cookie-secret' not in json.dumps(store.run(run['id']), ensure_ascii=False)
        parsed = store.create_job('keywords', {'description': '采集这位创作者', 'platforms': ['xhs']}, 'test', {})
        store.update_job(parsed['id'], status='completed', result={'questions': [], 'crawl_plan': {'xhs': {'crawler_type': 'creator', 'targets': ['creator123']}}})
        body = RunRequest(description='采集这位创作者', platforms=['xhs'], keyword_job_id=parsed['id'], targets={'xhs': ['creator123']})
        await worker.launch(body)
        await worker.task
        assert captured[-1].crawler_type.value == 'creator'
        assert captured[-1].creator_ids == 'creator123'
        with pytest.raises(ValueError, match='不匹配'):
            RunRequest(platforms=['xhs'], targets={'xhs': ['https://evil.invalid/video/1']})
    asyncio.run(check())


def test_ai_crawl_plan_allows_direct_targets_without_keywords():
    result = {'understanding': '采集指定帖子', 'product': '根据目标内容发现', 'audience': '', 'questions': [],
              'keywords': {'xhs': []}, 'crawl_plan': {'xhs': {'crawler_type': 'detail', 'targets': ['https://www.xiaohongshu.com/explore/abc']}}}
    assert ResearchAI.keyword_result(result, ['xhs'])['crawl_plan']['xhs']['crawler_type'] == 'detail'
    with pytest.raises(ValueError):
        ResearchAI.keyword_result({**result, 'crawl_plan': {'xhs': {'crawler_type': 'invalid', 'targets': []}}}, ['xhs'])


def test_direct_capture_skips_old_detail_and_comments(store, monkeypatch):
    import config
    from tools import research_capture as capture
    from media_platform.xhs.core import XiaoHongShuCrawler
    rid = batch(store)
    with store.connect() as db:
        store.library.upsert(db, 'xhs', {'note_id': 'old', 'title': '旧帖子'})
    monkeypatch.setenv('MC_RESEARCH_ID', rid)
    monkeypatch.setattr(capture, 'context', lambda: (store, rid))
    monkeypatch.setattr(config, 'CRAWLER_TYPE', 'detail')
    monkeypatch.setattr(config, 'PLATFORM', 'xhs')
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    monkeypatch.setattr(config, 'ENABLE_GET_COMMENTS', True)
    crawler = XiaoHongShuCrawler.__new__(XiaoHongShuCrawler)
    crawler.xhs_client = SimpleNamespace(get_note_by_id=AsyncMock(return_value={'note_id': 'new'}))
    crawler.get_comments = AsyncMock()
    async def check():
        assert await crawler.get_note_detail_async_task('old', 'pc_search', 'old-token', asyncio.Semaphore(1)) is None
        crawler.xhs_client.get_note_by_id.assert_not_awaited()
        assert (await crawler.get_note_detail_async_task('new', 'pc_search', 'new-token', asyncio.Semaphore(1)))['note_id'] == 'new'
        store.save_item(rid, 'xhs', {'note_id': 'new', 'title': '新帖子'})
        await crawler.batch_get_note_comments(['old', 'new'], ['old-token', 'new-token'])
        assert crawler.get_comments.await_count == 1
        assert crawler.get_comments.await_args.kwargs['note_id'] == 'new'
        assert crawler.get_comments.await_args.kwargs['xsec_token'] == 'new-token'
    asyncio.run(check())


def test_old_console_urls_redirect_and_source_is_retained():
    from api.main import app
    from pathlib import Path
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for path in ('/', '/index.html', '/static/index.html'):
                response = await client.get(path)
                assert response.status_code == 307
                assert response.headers['location'] == '/research'
    asyncio.run(check())
    assert (Path(__file__).resolve().parents[1] / 'webui/src/App.tsx').exists()


def test_research_native_file_output_keeps_batch_id(store, tmp_path, monkeypatch):
    import config
    from tools import research_capture as capture
    from tools.async_file_writer import AsyncFileWriter
    rid = batch(store)
    monkeypatch.setenv('MC_RESEARCH_ID', rid)
    monkeypatch.setattr(capture, 'context', lambda: (store, rid))
    monkeypatch.setattr(config, 'SAVE_DATA_OPTION', 'jsonl')
    monkeypatch.setattr(config, 'SAVE_DATA_PATH', str(tmp_path / 'output'))
    assert store.candidate(rid, 'xhs', 'file-post', '词', 50)
    row = {'note_id': 'file-post', 'title': '原生文件输出'}
    assert capture.save('xhs', row) is False  # continue through the existing file writer
    assert row['run_id'] == rid
    writer = AsyncFileWriter('xhs', 'search')
    asyncio.run(writer.write_to_jsonl(row, 'contents'))
    paths = list((tmp_path / 'output').rglob('*.jsonl'))
    assert len(paths) == 1 and rid in paths[0].name
    assert json.loads(paths[0].read_text(encoding='utf-8'))['run_id'] == rid
    assert capture.save('xhs', row) is True  # duplicates do not reach the file writer


def test_bilibili_direct_bv_uses_canonical_id_for_dedup(store, monkeypatch):
    import config
    from tools import research_capture as capture
    from media_platform.bilibili.core import BilibiliCrawler
    rid = batch(store, ['bili'])
    with store.connect() as db:
        store.library.upsert(db, 'bili', {'video_id': '123', 'title': '旧视频'})
    monkeypatch.setenv('MC_RESEARCH_ID', rid)
    monkeypatch.setattr(capture, 'context', lambda: (store, rid))
    monkeypatch.setattr(config, 'CRAWLER_TYPE', 'detail')
    monkeypatch.setattr(config, 'PLATFORM', 'bili')
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    crawler = BilibiliCrawler.__new__(BilibiliCrawler)
    crawler.bili_client = SimpleNamespace(get_video_info=AsyncMock(side_effect=[{'View': {'aid': 123}}, {'View': {'aid': 124}}]))
    async def check():
        assert await crawler.get_video_info_task(0, 'BVold', asyncio.Semaphore(1)) is None
        assert (await crawler.get_video_info_task(0, 'BVnew', asyncio.Semaphore(1)))['View']['aid'] == 124
    asyncio.run(check())
    counts = store.run(rid)['counts']
    assert counts['checked'] == 2 and counts['old_posts'] == 1


def test_process_start_log_redacts_cookie(monkeypatch):
    from api.services.crawler_manager import CrawlerManager
    import importlib
    module = importlib.import_module('api.services.crawler_manager')
    manager = CrawlerManager()
    manager._read_output = AsyncMock()
    received = []
    def popen(cmd, **kwargs):
        received.extend(cmd)
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    from api.schemas import CrawlerStartRequest
    async def check():
        assert await manager.start(CrawlerStartRequest(platform='xhs', keywords='--cookies', cookies='cookie-must-stay-secret'))
        await manager._read_task
    asyncio.run(check())
    assert 'cookie-must-stay-secret' in received
    assert 'cookie-must-stay-secret' not in '\n'.join(entry.message for entry in manager.logs)


def test_creator_pagination_stops_repeated_responses(monkeypatch):
    import config
    from tools import research_capture as capture
    from media_platform.douyin.client import DouYinClient
    from media_platform.bilibili.core import BilibiliCrawler
    monkeypatch.setattr(capture, 'active', lambda: True)
    monkeypatch.setattr(capture, 'exhausted', lambda: False)
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    dy = DouYinClient.__new__(DouYinClient)
    dy.get_user_aweme_posts = AsyncMock(return_value={'has_more': 1, 'max_cursor': 'same', 'aweme_list': [{'aweme_id': '1'}]})
    bili = BilibiliCrawler.__new__(BilibiliCrawler)
    bili.bili_client = SimpleNamespace(get_creator_videos=AsyncMock(return_value={'list': {'vlist': [{'bvid': 'BVsame'}]}, 'page': {'count': 100000}}))
    bili.get_specified_videos = AsyncMock()
    async def check():
        await dy.get_all_user_aweme_posts('creator', AsyncMock())
        await bili.get_creator_videos(1)
    asyncio.run(check())
    assert dy.get_user_aweme_posts.await_count == 2
    assert bili.bili_client.get_creator_videos.await_count == 2
    assert bili.get_specified_videos.await_count == 1
