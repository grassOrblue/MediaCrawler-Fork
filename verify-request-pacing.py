"""No network requests: validate throttling, large-job policy and stop signals."""
import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import patch, AsyncMock
import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))
os.chdir(ROOT / 'app')
os.environ['MPLCONFIGDIR'] = str(ROOT / 'cache/matplotlib')
from tools.request_pacer import RequestPacer
from api.schemas.crawler import CrawlerStartRequest
from api.services.crawler_manager import CrawlerManager
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.exception import PlatformAccessError, IPBlockError


class Clock:
    now = 0.0
    async def sleep(self, seconds):
        self.now += seconds


async def main():
    clock = Clock()
    pacer = RequestPacer(2, 2, 10, clock=lambda: clock.now, sleep=clock.sleep)
    starts = []
    async def send():
        await pacer.wait()
        starts.append(clock.now)
    await asyncio.gather(send(), send(), send())
    assert starts == [0, 2, 12], starts
    settings = CrawlerManager._runtime_settings
    assert settings(CrawlerStartRequest(platform='xhs', max_notes_count=20))['MC_REQUEST_INTERVAL'] == '2'
    for fields in [{'max_notes_count':100001}, {'stop_condition':'exhausted'}, {'rate_mode':'gentle'},
                   {'max_notes_count':100, 'max_comments_count':100}]:
        effective = settings(CrawlerStartRequest(platform='xhs', **fields))
        assert float(effective['MC_REQUEST_INTERVAL']) >= 5
        assert effective['MC_REST_EVERY'] == '100'
    assert settings(CrawlerStartRequest(platform='xhs', rate_mode='gentle', request_interval=20, rest_every=50, rest_seconds=120)) == {
        'MC_REQUEST_INTERVAL':'20.0', 'MC_REST_EVERY':'50', 'MC_REST_SECONDS':'120.0'}
    CrawlerStartRequest(platform='xhs', max_notes_count=1000000, max_comments_count=1000000)
    try:
        CrawlerStartRequest(platform='xhs', max_notes_count=1000001)
    except ValidationError:
        pass
    else:
        raise AssertionError('API maximum must be validated')
    for status, code in [(401,None),(403,None),(429,None),(461,None),(471,None),(200,300011),(200,300012)]:
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(status, json={'success':False,'code':code}, headers={'Retry-After':'120'})
        transport = httpx.MockTransport(respond)
        client = XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})
        client._pacer = RequestPacer(1, clock=lambda:clock.now, sleep=clock.sleep)
        with patch('media_platform.xhs.client.make_async_client', side_effect=lambda **kw: httpx.AsyncClient(transport=transport)):
            for attempt in range(2):
                try:
                    await client.request('GET', 'https://local-test.invalid/test')
                except (PlatformAccessError, IPBlockError):
                    pass
                else:
                    raise AssertionError('Restriction must stop requests')
        assert len(calls) == 1, (status, code, len(calls))
        if status == 429:
            assert '120' in client._pacer.blocked_reason
    attempts = []
    def transient(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(500, json={'success':False, 'code':500, 'msg':'temporary test failure'})
        return httpx.Response(200, json={'success':True, 'data':{'ok':True}})
    client = XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})
    client._pacer = RequestPacer(2, clock=lambda:clock.now, sleep=clock.sleep)
    retry_sleep = AsyncMock()
    with patch('media_platform.xhs.client.make_async_client', side_effect=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(transient))), \
         patch.object(XiaoHongShuClient.request.retry, 'sleep', retry_sleep):
        assert await client.request('GET','https://local-test.invalid/transient') == {'ok':True}
    assert len(attempts) == 3
    assert [float(call.args[0]) for call in retry_sleep.await_args_list] == [2,4]
    print('PASS: concurrent pacing, periodic rest, large-job policy, million-item limits, restriction stop and exponential transient retry')


asyncio.run(main())
