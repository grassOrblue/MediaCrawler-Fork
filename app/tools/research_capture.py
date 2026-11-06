"""Opt-in research path. Normal crawler/storage behavior is left intact."""
import asyncio
import math
import os
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
    store.save_item(rid, platform, row, comment)
    return True


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
    limit = run['config']['max_posts']
    page_size = {'xhs': 20, 'dy': 10, 'bili': 20}[platform]
    semaphore = asyncio.Semaphore(1)
    for keyword in run['keywords'][platform]:
        source_keyword_var.set(keyword)
        prior_pages = set()
        search_id = ''
        for page in range(1, math.ceil(limit / page_size) + 2):
            task = next(t for t in store.run(rid, jobs=False)['tasks'] if t['platform'] == platform)
            if task['checked'] >= limit:
                return
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
            if not rows:
                break
            signature = tuple(ids)
            if signature in prior_pages:
                break
            prior_pages.add(signature)
            for row, pid in zip(rows, ids):
                if not store.candidate(rid, platform, pid, keyword, limit):
                    continue
                if platform == 'xhs':
                    from store import xhs as writer
                    detail = await crawler.get_note_detail_async_task(note_id=pid, xsec_source=row.get('xsec_source'),
                               xsec_token=row.get('xsec_token'), semaphore=semaphore)
                    if not detail:
                        store.failure(rid, platform)
                        continue
                    await writer.update_xhs_note(detail)
                    await crawler.batch_get_note_comments([pid], [detail.get('xsec_token') or row.get('xsec_token')])
                elif platform == 'dy':
                    from store import douyin as writer
                    await writer.update_douyin_aweme(row)
                    await crawler.batch_get_note_comments([pid])
                else:
                    from store import bilibili as writer
                    detail = await crawler.get_video_info_task(aid=pid, bvid='', semaphore=semaphore)
                    if not detail:
                        store.failure(rid, platform)
                        continue
                    await writer.update_bilibili_video(detail)
                    await crawler.batch_get_video_comments([pid])
            if response.get('has_more') in (False, 0):
                break
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
