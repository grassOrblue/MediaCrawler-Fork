"""Deployed access policy: stop on account/IP restrictions, without HTML fallback."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.exception import IPBlockError, PlatformAccessError


def make_crawler(client):
    crawler = XiaoHongShuCrawler.__new__(XiaoHongShuCrawler)
    crawler.xhs_client = client
    return crawler


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [IPBlockError('IP restricted'), PlatformAccessError('HTTP 403')])
async def test_note_detail_propagates_access_error(error):
    client = AsyncMock()
    client.get_note_by_id.side_effect = error
    with pytest.raises(type(error)):
        await make_crawler(client).get_note_detail_async_task('n1', 'pc_search', 'token', asyncio.Semaphore(1))
    client.get_note_by_id_from_html.assert_not_awaited()


@pytest.mark.asyncio
async def test_gather_propagates_restriction(monkeypatch):
    monkeypatch.setattr(config, 'CRAWLER_MAX_SLEEP_SEC', 0)
    client = AsyncMock()
    async def detail(note_id, *args, **kwargs):
        if note_id == 'blocked':
            raise PlatformAccessError('account restricted')
        return {'note_id': note_id}
    client.get_note_by_id.side_effect = detail
    crawler = make_crawler(client)
    tasks = [asyncio.create_task(crawler.get_note_detail_async_task(pid, 'pc_search', 'token', asyncio.Semaphore(1))) for pid in ('ok', 'blocked')]
    with pytest.raises(PlatformAccessError):
        await asyncio.gather(*tasks)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[1], PlatformAccessError)
    client.get_note_by_id_from_html.assert_not_awaited()


@pytest.mark.asyncio
async def test_creator_restriction_stops_further_requests(monkeypatch):
    monkeypatch.setattr(config, 'XHS_CREATOR_ID_LIST', [
        'https://www.xiaohongshu.com/user/profile/blocked?xsec_token=a&xsec_source=pc_feed',
        'https://www.xiaohongshu.com/user/profile/next?xsec_token=b&xsec_source=pc_feed'])
    client = AsyncMock()
    client.get_creator_info.side_effect = PlatformAccessError('HTTP 403')
    with pytest.raises(PlatformAccessError):
        await make_crawler(client).get_creators_and_notes()
    assert client.get_creator_info.await_count == 1
    client.get_all_notes_by_creator.assert_not_awaited()
