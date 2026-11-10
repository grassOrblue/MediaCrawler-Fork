"""Opt-in research path. Normal crawler/storage behavior is left intact."""
import asyncio
import math
import os
from itertools import count
from functools import lru_cache

from api.services.research_store import ResearchStore


def active():
    return bool(os.environ.get('MC_RESEARCH_ID'))


@lru_cache(maxsize=1)
def context():
    store = ResearchStore(os.environ['MC_RESEARCH_HOME'])
    rid = os.environ['MC_RESEARCH_ID']
    return store, rid


def save(platform, row, comment=False):
    if not active():
        return False
    store, rid = context()
    saved = store.save_item(rid, platform, row, comment)
    if not saved:
        return True
    import config
    if config.SAVE_DATA_OPTION in ('json', 'jsonl', 'csv', 'excel'):
        row['run_id'] = rid
    return config.SAVE_DATA_OPTION == 'sqlite'


def exhausted():
    if not active():
        return False
    store, rid = context()
    import config
    run = store.run(rid, jobs=False)
    limit = run['config']['max_posts']
    task = next(t for t in run['tasks'] if t['platform'] == config.PLATFORM)
    return bool(limit and task['checked'] >= limit and not store.retry_items(rid, config.PLATFORM))


def detail_allowed(platform, pid):
    import config
    if not active() or config.CRAWLER_TYPE == 'search':
        return True
    store, rid = context()
    return store.candidate(rid, platform, str(pid or ''), '直接采集', store.run(rid, jobs=False)['config']['max_posts'])


def new_ids(platform, ids):
    if not active():
        return ids
    store, rid = context()
    with store.connect() as db:
        saved = {r[0] for r in db.execute("SELECT i.item_id FROM research_items i LEFT JOIN research_work w ON w.run_id=i.run_id AND w.platform=i.platform AND w.post_id=i.item_id WHERE i.run_id=? AND i.platform=? AND i.kind='post' AND i.is_new=1 AND coalesce(w.status,'pending')!='completed'", (rid, platform))}
    return [pid for pid in ids if str(pid) in saved]


def complete(platform, ids):
    if active():
        store, rid = context()
        for pid in ids:
            store.finish_item(rid, platform, pid)


def failed(platform, exc):
    if active():
        store, rid = context()
        store.failure(rid, platform)
        store.set_platform(rid, platform, 'failed', type(exc).__name__ + ': ' + str(exc)[:500])


async def search(crawler, platform):
    """Three small platform adapters share the candidate budget and ID dedup gate."""
    import config
    from var import source_keyword_var
    store, rid = context()
    run = store.run(rid)
    limit = 0 if run['config'].get('stop_condition') == 'exhausted' else run['config']['max_posts']
    page_size = {'xhs': 20, 'dy': 10, 'bili': 20}[platform]
    for work in store.retry_items(rid, platform):
        source_keyword_var.set(work['keyword'])
        if store.candidate(rid, platform, work['post_id'], work['keyword'], limit):
            await capture_row(crawler, platform, work['data'], work['post_id'])
    saved = store.checkpoint(rid, platform, 'search')
    first_page = run['config'].get('start_page', 1)
    for keyword_index, keyword in enumerate(run['keywords'][platform]):
        if keyword_index < saved.get('keyword', 0):
            continue
        source_keyword_var.set(keyword)
        prior_pages = set()
        search_id = ''
        start_page = saved.get('page', first_page) if keyword_index == saved.get('keyword') else first_page
        for page in (range(start_page, first_page + math.ceil(limit / page_size) + 1) if limit else count(start_page)):
            task = next(t for t in store.run(rid, jobs=False)['tasks'] if t['platform'] == platform)
            if limit and task['checked'] >= limit:
                return
            if saved.get('keyword') == keyword_index and saved.get('page') == page and 'rows' in saved:
                rows, ids, response = saved['rows'], saved['ids'], {'has_more': saved.get('has_more')}
            else:
                if platform == 'xhs':
                    from media_platform.xhs.field import SearchSortType
                    from media_platform.xhs.help import get_search_id
                    search_id = search_id or get_search_id()
                    response = await crawler.xhs_client.get_note_by_keyword(keyword=keyword, search_id=search_id, page=page, sort=SearchSortType.GENERAL)
                    rows = response.get('items', [])
                    rows = [r for r in rows if r.get('model_type') not in ('rec_query', 'hot_query')]
                    ids = [str(r.get('id') or '') for r in rows]
                elif platform == 'dy':
                    from media_platform.douyin.field import PublishTimeType
                    response = await crawler.dy_client.search_info_by_keyword(keyword=keyword, offset=(page - 1) * page_size,
                                  publish_time=PublishTimeType(config.PUBLISH_TIME_TYPE), search_id=search_id)
                    if 'data' not in response or response['data'] is None:
                        raise ValueError('抖音搜索响应异常，可能需要重新登录或处理平台限制')
                    rows = []
                    for item in response['data']:
                        info = item.get('aweme_info')
                        if not info:
                            mixed = (item.get('aweme_mix_info') or {}).get('mix_items') or []
                            info = mixed[0] if mixed else {}
                        rows.append(info)
                    ids = [str(r.get('aweme_id') or '') for r in rows]
                    search_id = (response.get('extra') or {}).get('logid', '')
                else:
                    from media_platform.bilibili.field import SearchOrderType
                    response = await crawler.bili_client.search_video_by_keyword(keyword=keyword, page=page, page_size=page_size,
                                  order=SearchOrderType.DEFAULT, pubtime_begin_s=0, pubtime_end_s=0)
                    if 'result' not in response:
                        raise ValueError('B站搜索响应异常，可能需要重新登录或处理平台限制')
                    rows = response.get('result') or []
                    ids = [str(r.get('aid') or '') for r in rows]
            position = {'keyword': keyword_index, 'page': page, 'rows': rows, 'ids': ids, 'has_more': response.get('has_more')}
            index_start = saved.get('index', 0) if saved.get('keyword') == keyword_index and saved.get('page') == page else 0
            store.checkpoint(rid, platform, 'search', {**position, 'index': index_start})
            if not rows:
                break
            signature = tuple(ids)
            if signature in prior_pages:
                break
            prior_pages.add(signature)
            for index in range(index_start, len(rows)):
                row, pid = rows[index], ids[index]
                if not store.candidate(rid, platform, pid, keyword, limit, data=row):
                    continue
                await capture_row(crawler, platform, row, pid)
                store.checkpoint(rid, platform, 'search', {**position, 'index': index + 1})
            store.checkpoint(rid, platform, 'search', {'keyword': keyword_index, 'page': page + 1})
            if response.get('has_more') in (False, 0):
                break
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
        store.checkpoint(rid, platform, 'search', {'keyword': keyword_index + 1, 'page': first_page})


async def capture_row(crawler, platform, row, pid):
    store, rid = context()
    semaphore = asyncio.Semaphore(1)
    if platform == 'xhs':
        from store import xhs as writer
        detail = await crawler.get_note_detail_async_task(note_id=pid, xsec_source=row.get('xsec_source'),
                   xsec_token=row.get('xsec_token'), semaphore=semaphore)
        if not detail:
            store.failure(rid, platform)
            return
        await writer.update_xhs_note(detail)
        await crawler.download_media(detail)
        await crawler.batch_get_note_comments([pid], [detail.get('xsec_token') or row.get('xsec_token')])
    elif platform == 'dy':
        from store import douyin as writer
        await writer.update_douyin_aweme(row)
        await crawler.download_media(aweme_item=row)
        await crawler.batch_get_note_comments([pid])
    else:
        from store import bilibili as writer
        detail = await crawler.get_video_info_task(aid=pid, bvid='', semaphore=semaphore)
        if not detail:
            store.failure(rid, platform)
            return
        await writer.update_bilibili_video(detail)
        await crawler.download_media(detail, semaphore)
        await crawler.batch_get_video_comments([pid])
    store.finish_item(rid, platform, pid)
