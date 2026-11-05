"""Test pagination and API/CLI limit wiring using synthetic posts only."""
import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
os.environ['MPLCONFIGDIR'] = str(ROOT / 'cache/matplotlib')
sys.path.insert(0, str(ROOT / 'app'))
os.chdir(ROOT / 'app')
import config
from api.schemas.crawler import CrawlerStartRequest
from api.services.crawler_manager import CrawlerManager
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.exception import DataFetchError


def result(ids, more=True):
    return {'items': [{'id': str(i), 'model_type': 'note'} for i in ids], 'has_more': more}


async def scenario(limit, pages, expected, keywords='test', start=1, raises=False):
    crawler = XiaoHongShuCrawler()
    fetch = AsyncMock(side_effect=pages)
    crawler.xhs_client = type('Client', (), {'get_note_by_keyword': fetch})()

    async def detail(note_id, **kwargs):
        return {'note_id': note_id, 'xsec_token': 'synthetic'}

    crawler.get_note_detail_async_task = AsyncMock(side_effect=detail)
    crawler.download_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    with patch.multiple(config, CRAWLER_MAX_NOTES_COUNT=limit, START_PAGE=start,
                        KEYWORDS=keywords, CRAWLER_MAX_SLEEP_SEC=0), \
         patch('media_platform.xhs.core.xhs_store.update_xhs_note', new_callable=AsyncMock) as save:
        if raises:
            try:
                await crawler.search()
            except DataFetchError:
                pass
            else:
                raise AssertionError('Fetch errors must propagate instead of reporting exhausted data')
        else:
            await crawler.search()
        ids = [call.args[0]['note_id'] for call in save.await_args_list]
        assert ids == [str(i) for i in expected], ids
        assert fetch.await_args_list[0].kwargs['page'] == start
    return fetch.await_count


async def main():
    assert await scenario(1, [result(range(20))], [0]) == 1
    assert await scenario(25, [result(range(20)), result(range(20,40))], range(25)) == 2
    assert await scenario(100, [result([1,2], more=False)], [1,2]) == 1
    assert await scenario(0, [result([1,2]), result([3], more=False)], [1,2,3]) == 2
    assert await scenario(0, [result([1]), result([1])], [1]) == 2
    assert await scenario(0, [result([], more=True)], []) == 1
    await scenario(1, [result([7]), result([8])], [7,8], keywords='first,second', start=3)
    await scenario(0, [DataFetchError('synthetic failure')], [], raises=True)
    manager = CrawlerManager()
    for mode, expected in [('count', '125'), ('exhausted', '0')]:
        request = CrawlerStartRequest(platform='xhs', max_notes_count=125,
                                      max_comments_count=35, stop_condition=mode)
        cmd = manager._build_command(request)
        assert cmd[cmd.index('--crawler_max_notes_count')+1] == expected
        assert cmd[cmd.index('--max_comments_count_singlenotes')+1] == '35'
    try:
        CrawlerStartRequest(platform='bili', stop_condition='exhausted')
    except ValidationError:
        pass
    else:
        raise AssertionError('Unsupported exhaustion mode must not be silently accepted')
    print('PASS: exact limits 1/25, last page, exhaustion, repeated/empty pages, start page, per-keyword counts, errors and API-to-CLI wiring')


asyncio.run(main())
