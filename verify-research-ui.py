"""Browser acceptance against temporary services and fake AI; never crawls real sites."""
import asyncio
import os
import json
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / 'browsers')
os.environ['TEMP'] = os.environ['TMP'] = str(ROOT / 'tmp')
from api.services.research_store import ResearchStore
from api.services.research_ai import SettingsFile, AISettings
from api.routers import research as routes
from api import main as main_module
from tests.test_research import fake_model


def verify(folder):
    store = ResearchStore(folder / 'home', folder / 'data')
    store.library.sqlite_source = None
    worker = routes.Workflow(store)
    worker.ai.settings = SettingsFile(folder / 'ai.json')
    worker.ai.settings.save(AISettings(api_key='ui-test-secret', model='mock-model', max_output_tokens=1024, context_tokens=32768))
    calls = []
    async def model(cfg, prompt):
        calls.append(prompt)
        if '分析研究描述' in prompt[0]['content']:
            data = json.loads(prompt[1]['content'])
            assert '还不知道具体产品' in data['description']
            return {'understanding': '探索大家在吐槽哪些日常物品及其具体问题', 'product': '不限品类（探索发现）', 'audience': '日常用品使用者',
                    'scenes': [], 'questions': [], 'keywords': {platform: [{'text': '买了最后悔的东西', 'reason': '发现物品与使用痛点'}] for platform in data['platforms']}}, {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}
        result, usage = await fake_model(cfg, prompt)
        if 'items' in result:
            for item in result['items']:
                item['name'] = '桌面支架底座太轻'
        return result, usage
    worker.ai.request = model
    with store.connect() as db:
        store.library.upsert(db, 'xhs', {'note_id': 'legacy', 'title': '历史无批次资料', 'desc': '旧内容'})
    prior = store.create_run({'name': '之前的批次', 'description': '之前的支架研究', 'platforms': ['xhs'], 'keywords': {'xhs': ['支架']}, 'config': {'max_posts': 50, 'max_comments': 50}})
    store.set_platform(prior['id'], 'xhs', 'running')
    store.candidate(prior['id'], 'xhs', 'previous', '支架', 50)
    store.save_item(prior['id'], 'xhs', {'note_id': 'previous', 'title': '上批支架', 'desc': '之前的正文'})
    store.set_platform(prior['id'], 'xhs', 'completed')
    store.set_run(prior['id'], 'completed')
    async def capture(body):
        run = store.create_run(body.model_dump())
        rid = run['id']
        store.set_run(rid, 'running')
        for platform in body.platforms:
            store.set_platform(rid, platform, 'running')
            pid = 'fixture-' + platform
            store.candidate(rid, platform, pid, body.keywords[platform][0], 50)
            store.save_item(rid, platform, {'note_id': pid, 'title': '桌面支架', 'desc': '底座太轻', 'note_url': 'https://example.invalid/' + pid})
            store.set_platform(rid, platform, 'completed', '本地模拟采集')
        store.set_run(rid, 'completed')
        return store.run(rid)
    worker.launch = capture
    routes.workflow = main_module.workflow = lambda: worker
    with patch('api.services.library_store.LibraryStore', return_value=store.library):
        from api import library_app
    library_app.store, library_app.HOME = store.library, folder / 'home'
    servers = [uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', timeout_graceful_shutdown=2))
               for app, port in [(main_module.app, 18092), (library_app.app, 18093)]]
    threads = [threading.Thread(target=server.run, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        for _ in range(100):
            if all(server.started for server in servers):
                break
            time.sleep(.1)
        assert all(server.started for server in servers), 'test servers did not start'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:18092/')
            expect(page.get_by_role('link', name='需求研究：描述目标、采集并分析')).to_have_attribute('href', '/research')
            page.goto('http://127.0.0.1:18092/research')
            page.get_by_role('button', name='AI设置', exact=True).click()
            expect(page.get_by_label('API Key', exact=True)).to_have_value('')
            assert 'ui-test-secret' not in page.content()
            page.get_by_role('button', name='开始研究', exact=True).click()
            page.get_by_label('想了解什么？', exact=True).fill('我还不知道具体产品，想看看大家最近在吐槽哪些日常物品')
            page.get_by_role('button', name='预估关键词解析用量', exact=True).click()
            expect(page.get_by_role('dialog')).to_be_visible()
            assert len(calls) == 0
            page.get_by_role('button', name='取消', exact=True).click()
            assert len(calls) == 0
            page.get_by_role('button', name='预估关键词解析用量', exact=True).click()
            page.get_by_role('button', name='确认发送并执行', exact=True).click()
            expect(page.get_by_label('小红书关键词（每行一组，最多三组）', exact=True)).to_have_value('买了最后悔的东西')
            expect(page.get_by_text('研究范围：不限品类（探索发现） · 人群：日常用品使用者', exact=True)).to_be_visible()
            assert len(calls) == 1
            page.get_by_role('button', name='检查完毕，启动本次采集', exact=True).click()
            expect(page.get_by_role('heading', name=re.compile(r'^\d{8}-\d{6}(?:-\d+)?$'))).to_be_visible()
            page.get_by_role('button', name='预估本次需求分析用量', exact=True).click()
            assert len(calls) == 1
            page.get_by_role('button', name='确认发送并执行', exact=True).click()
            expect(page.get_by_role('heading', name='1. 桌面支架底座太轻', exact=True)).to_be_visible(timeout=15000)
            expect(page.get_by_text('3 次提及', exact=True)).to_be_visible()
            page.get_by_text('核对 3 份原文证据', exact=True).click()
            expect(page.locator('.research-demand blockquote')).to_have_count(3)
            with page.expect_download() as downloaded:
                page.get_by_role('link', name='导出需求和证据 CSV', exact=True).click()
            csv = Path(downloaded.value.path()).read_text(encoding='utf-8-sig')
            rid = store.runs()[0]['id']
            assert rid in csv and '底座太轻' in csv
            page.screenshot(path=str(ROOT / 'research-desktop.png'), full_page=True)
            for width in (1024, 390):
                page.set_viewport_size({'width': width, 'height': 850})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'research page horizontal overflow'
            page.screenshot(path=str(ROOT / 'research-mobile.png'), full_page=True)
            page.get_by_role('button', name='预估本次需求分析用量', exact=True).click()
            expect(page.get_by_role('dialog')).to_be_visible()
            box = page.get_by_role('dialog').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= 391
            page.screenshot(path=str(ROOT / 'research-budget-mobile.png'))
            page.get_by_role('button', name='取消', exact=True).click()
            requests = []
            page.on('request', lambda request: requests.append(request.url) if urlsplit(request.url).path == '/api/library/posts' else None)
            page.goto('http://127.0.0.1:18093/')
            selector = page.get_by_role('combobox', name='采集批次', exact=True)
            expect(selector).to_have_value(rid)
            expect(page.locator('.lib-card')).to_have_count(3)
            assert requests and all(parse_qs(urlsplit(url).query).get('run_id') == [rid] for url in requests)
            assert selector.bounding_box()['y'] < page.get_by_label('包含关键词', exact=True).bounding_box()['y']
            selector.select_option(prior['id'])
            expect(page.locator('.lib-title')).to_have_text(['上批支架'])
            expect(page.get_by_role('link', name='导出筛选结果', exact=True)).to_have_attribute('href', re.compile('run_id=' + prior['id']))
            page.reload()
            expect(selector).to_have_value(prior['id'])
            expect(page.locator('.lib-title')).to_have_text(['上批支架'])
            selector.select_option('')
            expect(page.locator('.lib-card')).to_have_count(5)
            page.reload()
            expect(selector).to_have_value('')
            expect(page.locator('.lib-card')).to_have_count(5)
            selector.select_option('__legacy__')
            expect(page.locator('.lib-title')).to_have_text(['历史无批次资料'])
            selector.select_option(rid)
            page.get_by_role('button', name='重置', exact=True).click()
            expect(selector).to_have_value(rid)
            expect(page.locator('.lib-card')).to_have_count(3)
            page.screenshot(path=str(ROOT / 'library-batch-mobile.png'), full_page=True)
            page.goto(f'http://127.0.0.1:18093/?run_id={rid}&platform=xhs&post_id=fixture-xhs')
            expect(page.get_by_role('heading', name='桌面支架', exact=True)).to_be_visible()
            page.get_by_role('button', name='返回筛选结果', exact=True).click()
            expect(page.get_by_role('combobox', name='采集批次', exact=True)).to_have_value(rid)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'library page horizontal overflow'
            assert not errors, errors
            assert page.evaluate("localStorage.getItem('mediacrawler_license_accepted')") is None
            browser.close()
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(10)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(dir=ROOT / 'tmp', prefix='research-ui-') as folder:
        verify(Path(folder))
    print('PASS: open discovery, local approval, timestamp batch IDs, evidence counts, CSV, latest batch default without all-content flash, batch switching/reload/all/legacy, library links, desktop/mobile layout; no real AI or crawl calls')
