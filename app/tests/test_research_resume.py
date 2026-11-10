import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.research_store import ResearchStore
from api.routers import research as routes
from tests.test_research import batch, post


@pytest.mark.parametrize('platform', ['xhs', 'dy', 'bili'])
@pytest.mark.parametrize('failure_stage', ['detail', 'comments'])
def test_resume_same_batch_pending_post_at_limit(tmp_path, monkeypatch, platform, failure_stage):
    import config
    from tools import research_capture as capture
    store = ResearchStore(tmp_path/'home', tmp_path/'data')
    rid = batch(store, [platform], max_posts=1)
    monkeypatch.setenv('MC_RESEARCH_ID', rid)
    monkeypatch.setattr(capture, 'context', lambda: (store, rid))
    monkeypatch.setattr(config, 'CRAWLER_TYPE', 'search')
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    monkeypatch.setattr(config, 'SAVE_DATA_OPTION', 'sqlite')
    failed = [False]
    async def search(**kwargs):
        rows = {'xhs':{'items':[{'id':'1'}]},'dy':{'data':[{'aweme_info':{'aweme_id':'1','desc':'正文','author':{},'statistics':{}}}]},'bili':{'result':[{'aid':'1'}]}}
        if platform == 'dy' and failure_stage == 'detail' and not failed[0]:
            failed[0] = True
            raise ValueError('search failed')
        return rows[platform] | {'has_more':False}
    async def detail(**kwargs):
        if failure_stage == 'detail' and not failed[0]:
            failed[0] = True
            raise ValueError('detail failed')
        return {'note_id':'1','title':'标题','desc':'正文','user':{}} if platform=='xhs' else {'View':{'aid':'1','title':'标题','desc':'正文','owner':{},'stat':{}}}
    async def comments(*args):
        if failure_stage == 'comments' and not failed[0]:
            store.save_item(rid, platform, {'note_id':'1','comment_id':'c1','content':'评论'}, comment=True)
            failed[0] = True
            raise ValueError('comments failed after save')
        store.save_item(rid, platform, {'note_id':'1','comment_id':'c1','content':'评论'}, comment=True)
    search_mock=AsyncMock(side_effect=search)
    crawler=SimpleNamespace(xhs_client=SimpleNamespace(get_note_by_keyword=search_mock),dy_client=SimpleNamespace(search_info_by_keyword=search_mock),bili_client=SimpleNamespace(search_video_by_keyword=search_mock),get_note_detail_async_task=AsyncMock(side_effect=detail),get_video_info_task=AsyncMock(side_effect=detail),batch_get_note_comments=AsyncMock(side_effect=comments),batch_get_video_comments=AsyncMock(side_effect=comments),download_media=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(capture.search(crawler, platform))
    store.set_platform(rid, platform, 'failed')
    store.set_run(rid, 'failed')
    store = ResearchStore(tmp_path/'home', tmp_path/'data')
    store.resume_run(rid)
    store.set_platform(rid, platform, 'running')
    asyncio.run(capture.search(crawler, platform))
    result=store.run(rid)
    assert result['counts']['checked']==1 and result['counts']['new_posts']==1
    assert result['counts']['new_comments']==1
    assert len(store.runs())==1 and store.runs()[0]['id']==rid
    with store.connect() as db:
        assert db.execute("SELECT status FROM research_work WHERE run_id=?", (rid,)).fetchone()[0]=='completed'
    assert search_mock.call_count == (2 if platform=='dy' and failure_stage=='detail' else 1)


def test_search_resumes_failed_page_not_first_page(tmp_path, monkeypatch):
    import config
    from tools import research_capture as capture
    store=ResearchStore(tmp_path/'home',tmp_path/'data')
    rid=batch(store, ['dy'], max_posts=25)
    monkeypatch.setenv('MC_RESEARCH_ID',rid)
    monkeypatch.setattr(capture,'context',lambda:(store,rid))
    monkeypatch.setattr(config,'CRAWLER_MAX_SLEEP_SEC',0)
    monkeypatch.setattr(config,'SAVE_DATA_OPTION','sqlite')
    calls=[]
    async def search(**kwargs):
        calls.append(kwargs['offset'])
        if len(calls)==2:
            raise ValueError('page 2 failed')
        return {'data':[{'aweme_info':{'aweme_id':str(kwargs['offset']),'author':{},'statistics':{}}}], 'has_more':len(calls)==1}
    crawler=SimpleNamespace(dy_client=SimpleNamespace(search_info_by_keyword=search),batch_get_note_comments=AsyncMock(),download_media=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(capture.search(crawler,'dy'))
    store.set_run(rid,'failed');store.set_platform(rid,'dy','failed')
    store.resume_run(rid);store.set_platform(rid,'dy','running')
    asyncio.run(capture.search(crawler,'dy'))
    assert calls[:3]==[0,10,10]
    assert store.run(rid)['counts']['new_posts']==2


def test_resume_skips_completed_platform_and_rejects_duplicate(tmp_path, monkeypatch):
    store=ResearchStore(tmp_path/'home',tmp_path/'data')
    rid=batch(store,['xhs','dy'])
    post(store,rid,'saved')
    store.finish_item(rid,'xhs','saved')
    store.set_platform(rid,'xhs','completed')
    store.set_platform(rid,'dy','failed');store.failure(rid,'dy')
    store.set_run(rid,'partial')
    worker=routes.Workflow(store)
    manager=routes.crawler_manager
    monkeypatch.setattr(manager,'research_owner',None)
    monkeypatch.setattr(manager,'process',None)
    monkeypatch.setattr(manager,'status','idle')
    monkeypatch.setattr(manager,'_read_task',None)
    monkeypatch.setattr(routes,'CAPTURE_FILE',tmp_path/'capture.json')
    calls=[]
    async def start(config, env):
        calls.append(config.platform.value)
        manager.process=SimpleNamespace(poll=lambda:0)
        manager._read_task=asyncio.create_task(asyncio.sleep(0))
        return True
    monkeypatch.setattr(manager,'start',start)
    async def check():
        resumed=await worker.resume(rid)
        assert resumed['id']==rid
        with pytest.raises(ValueError,match='已有采集'):
            await worker.resume(rid)
        await worker.task
    asyncio.run(check())
    assert calls==['dy']
    assert store.run(rid)['status']=='completed'
    assert store.run(rid)['counts']['new_posts']==1
    with pytest.raises(ValueError):
        store.resume_run(rid)


def test_legacy_resume_releases_unsaved_seen_id_without_resetting_count(tmp_path):
    store=ResearchStore(tmp_path/'home',tmp_path/'data')
    rid=batch(store,['xhs'],max_posts=1)
    store.candidate(rid,'xhs','failed-id','词',1)
    with store.connect() as db:
        db.execute('DELETE FROM research_work')
    store.set_run(rid,'interrupted');store.set_platform(rid,'xhs','interrupted')
    store.resume_run(rid);store.set_platform(rid,'xhs','running')
    assert store.candidate(rid,'xhs','failed-id','词',1)
    assert not store.candidate(rid,'xhs','failed-id','词',1)
    assert store.run(rid)['counts']['checked']==1


@pytest.mark.parametrize('platform',['xhs','dy','bili'])
def test_direct_resume_allows_pending_id_at_cap_but_skips_finished(tmp_path,monkeypatch,platform):
    import config
    from tools import research_capture as capture
    store=ResearchStore(tmp_path/'home',tmp_path/'data')
    rid=batch(store,[platform],max_posts=2)
    for pid in ('done','pending'):
        post(store,rid,pid,platform=platform)
    store.finish_item(rid,platform,'done')
    store.set_run(rid,'failed');store.set_platform(rid,platform,'failed')
    store.resume_run(rid);store.set_platform(rid,platform,'running')
    monkeypatch.setenv('MC_RESEARCH_ID',rid)
    monkeypatch.setattr(capture,'context',lambda:(store,rid))
    monkeypatch.setattr(config,'CRAWLER_TYPE','detail')
    monkeypatch.setattr(config,'PLATFORM',platform)
    assert not capture.exhausted()
    assert not capture.detail_allowed(platform,'done')
    assert capture.detail_allowed(platform,'pending')
    assert capture.new_ids(platform,['done','pending'])==['pending']
    capture.complete(platform,['pending'])
    assert capture.exhausted() and capture.new_ids(platform,['done','pending'])==[]
