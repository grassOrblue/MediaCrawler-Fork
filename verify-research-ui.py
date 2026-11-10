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
                    'scenes': [], 'questions': [], 'keywords': {platform: [{'text': '买了最后悔的东西', 'reason': '发现物品与使用痛点'}, {'text': {'xhs':'闲置原因','dy':'使用吐槽','bili':'产品缺点'}[platform], 'reason':'补充不同诉求'}] for platform in data['platforms']}}, {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}
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
            store.candidate(rid, platform, pid, (body.keywords.get(platform) or ['直接采集'])[0], 50)
            store.save_item(rid, platform, {'note_id': pid, 'title': '桌面支架', 'desc': '底座太轻', 'note_url': 'https://example.invalid/' + pid})
            store.set_platform(rid, platform, 'completed', '本地模拟采集')
        store.set_run(rid, 'completed')
        return store.run(rid)
    worker.launch = capture
    routes.workflow = main_module.workflow = lambda: worker
    routes.CAPTURE_FILE = folder / 'capture.json'
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
            assert page.url.endswith('/research')
            assert page.get_by_role('link', name='采集控制台', exact=True).count()==0
            page.goto('http://127.0.0.1:18092/static/index.html')
            assert page.url.endswith('/research')
            page.get_by_text('系统控制台 · 日志与运行状态',exact=True).click()
            expect(page.get_by_role('button',name='停止当前采集',exact=True)).to_be_visible()
            page.get_by_role('button',name='通用设置',exact=True).click()
            page.get_by_label('每平台检查帖子上限',exact=True).fill('77')
            page.get_by_role('button',name='保存通用设置',exact=True).click()
            expect(page.get_by_role('status').filter(has_text='通用设置已保存，后续采集统一使用。')).to_be_visible()
            assert routes.capture_preferences().max_posts==77
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
            shared_words = page.get_by_label('共享关键词（每行一组，可手动追加）', exact=True)
            expect(shared_words).to_have_value('买了最后悔的东西\n闲置原因\n使用吐槽\n产品缺点')
            assert page.locator('textarea[aria-label*="关键词"]').count()==1
            page.get_by_text('查看目标解析',exact=True).click()
            expect(page.get_by_text('研究范围：不限品类（探索发现） · 人群：日常用品使用者', exact=True)).to_be_visible()
            page.get_by_text('查看目标解析',exact=True).click()
            assert len(calls) == 1
            panels=page.locator('.research-start>.research-panel')
            assert abs(panels.nth(0).bounding_box()['height']-panels.nth(1).bounding_box()['height'])<2
            page.screenshot(path=str(ROOT / 'unified-research-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':850})
            page.get_by_text('系统控制台 · 日志与运行状态',exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'unified start page horizontal overflow'
            page.screenshot(path=str(ROOT / 'unified-research-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1440,'height':1000})
            page.get_by_role('button',name='改为手动填写（保留当前内容）',exact=True).click()
            shared_words.fill('买了最后悔的东西\n手动追加')
            with page.expect_request(lambda request: request.url.endswith('/api/research/runs') and request.method=='POST') as shared_start:
                page.get_by_role('button', name='检查完毕，启动本次采集', exact=True).click()
            assert shared_start.value.post_data_json['keywords']=={platform:['买了最后悔的东西','手动追加'] for platform in ('xhs','dy','bili')}
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
            report_id = store.run(rid)['jobs'][0]['id']
            saved = store.job(report_id)
            warning = '归类编号已校正：测试候选独立保留，同义需求可能分开计数。'
            store.update_job(report_id, result={**saved['result'], 'warnings': [warning]})
            page.reload()
            page.get_by_role('button', name='批次与报告', exact=True).click()
            page.locator('.research-run').filter(has_text=rid).click()
            expect(page.get_by_text(warning, exact=True)).to_be_visible()
            with page.expect_download() as warned_download:
                page.get_by_role('link', name='导出需求和证据 CSV', exact=True).click()
            assert warning in Path(warned_download.value.path()).read_text(encoding='utf-8-sig')
            page.get_by_label('分析版本', exact=True).select_option(report_id)
            page.get_by_role('button', name='预估本次需求分析用量', exact=True).click()
            page.get_by_role('button', name='确认发送并执行', exact=True).click()
            expect(page.get_by_label('分析版本', exact=True)).not_to_have_value(report_id, timeout=15000)
            expect(page.get_by_role('heading', name='1. 桌面支架底座太轻', exact=True)).to_be_visible(timeout=15000)
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
            page.goto('http://127.0.0.1:18092/research')
            page.get_by_label('共享关键词（每行一组，可手动追加）',exact=True).fill('手动添加关键词')
            expect(page.get_by_role('tab')).to_have_count(3)
            page.get_by_role('tab',name='抖音',exact=True).click()
            page.get_by_label('抖音目标链接 / ID',exact=True).fill('https://www.douyin.com/video/fixture')
            assert page.get_by_label('小红书目标链接 / ID',exact=True).count()==0
            page.get_by_role('tab',name='小红书',exact=True).click()
            expect(page.get_by_label('小红书目标链接 / ID',exact=True)).to_have_value('')
            page.get_by_role('tab',name='抖音',exact=True).click()
            expect(page.get_by_label('抖音目标链接 / ID',exact=True)).to_have_value('https://www.douyin.com/video/fixture')
            page.get_by_role('checkbox',name='抖音',exact=True).uncheck()
            expect(page.get_by_role('tab')).to_have_count(2)
            expect(page.get_by_role('tab',name='小红书',exact=True)).to_have_attribute('aria-selected','true')
            page.get_by_role('checkbox',name='抖音',exact=True).check()
            page.get_by_role('tab',name='抖音',exact=True).click()
            expect(page.get_by_label('抖音目标链接 / ID',exact=True)).to_have_value('https://www.douyin.com/video/fixture')
            page.get_by_label('抖音目标链接 / ID',exact=True).fill('')
            for platform in ('小红书','抖音','B站'):
                page.get_by_role('checkbox',name=platform,exact=True).uncheck()
            expect(page.get_by_role('tab')).to_have_count(0)
            expect(page.get_by_role('button',name='检查完毕，启动本次采集',exact=True)).to_be_disabled()
            for platform in ('小红书','抖音','B站'):
                page.get_by_role('checkbox',name=platform,exact=True).check()
            before=len(calls)
            with page.expect_request(lambda request: request.url.endswith('/api/research/runs') and request.method=='POST') as started:
                page.get_by_role('button',name='检查完毕，启动本次采集',exact=True).click()
            assert started.value.post_data_json['keyword_job_id']==''
            assert started.value.post_data_json['keywords']=={platform:['手动添加关键词'] for platform in ('xhs','dy','bili')}
            expect(page.get_by_role('heading',name='需求报告',exact=True)).to_be_visible()
            assert len(calls)==before
            page.goto('http://127.0.0.1:18092/research')
            page.get_by_role('checkbox',name='抖音',exact=True).uncheck()
            page.get_by_role('checkbox',name='B站',exact=True).uncheck()
            page.get_by_role('combobox',name='采集类型',exact=True).select_option('creator')
            page.get_by_label('小红书目标链接 / ID',exact=True).fill('https://www.xiaohongshu.com/user/profile/fixture')
            with page.expect_request(lambda request: request.url.endswith('/api/research/runs') and request.method=='POST') as creator:
                page.get_by_role('button',name='检查完毕，启动本次采集',exact=True).click()
            assert creator.value.post_data_json['crawler_type']=='creator'
            assert creator.value.post_data_json['targets']['xhs']==['https://www.xiaohongshu.com/user/profile/fixture']
            expect(page.get_by_role('heading',name='需求报告',exact=True)).to_be_visible()
            assert len(calls)==before
            assert not errors, errors
            store.set_platform(rid,'xhs','failed','模拟中断')
            store.set_run(rid,'partial','模拟中断')
            async def resume_fixture(run_id):
                store.resume_run(run_id)
                store.set_platform(run_id,'xhs','running')
                store.candidate(run_id,'xhs','resumed-post','续采',50)
                store.save_item(run_id,'xhs',{'note_id':'resumed-post','title':'续采新增帖子','desc':'新增正文'})
                store.finish_item(run_id,'xhs','resumed-post')
                store.set_platform(run_id,'xhs','completed')
                store.set_run(run_id,'completed')
                return store.run(run_id)
            worker.resume=resume_fixture
            page.get_by_role('button',name='批次与报告',exact=True).click()
            page.locator('.research-run').filter(has_text=rid).click()
            run_count=len(store.runs())
            with page.expect_request(lambda request: request.url.endswith('/runs/'+rid+'/resume') and request.method=='POST'):
                page.get_by_role('button',name='继续采集（同一批次）',exact=True).click()
            expect(page.get_by_text('此报告使用续采前的材料快照；续采结束后请重新预估分析，生成包含新增内容的报告版本。',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='继续采集（同一批次）',exact=True)).to_have_count(0)
            assert len(store.runs())==run_count and store.run(rid)['counts']['new_posts']==4
            qr = {'id':'qr1', 'platform':'xhs', 'attempt':1, 'phase':'scanning', 'image':'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='}
            login_state = [qr]
            actions = []
            page.route('**/api/research/login', lambda route: route.fulfill(body=json.dumps(login_state[0]),content_type='application/json'))
            def login_action(route):
                command=route.request.post_data_json
                actions.append(command)
                login_state[0] = {**qr, 'id':'qr2', 'attempt':2} if command['action']=='refresh' else None
                route.fulfill(json={'accepted':True})
            page.route('**/api/research/login/action',login_action)
            page.reload()
            expect(page.get_by_role('dialog')).to_be_visible()
            expect(page.get_by_alt_text('小红书登录二维码')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog')).to_be_visible()
            page.get_by_role('button',name='刷新二维码',exact=True).click()
            expect(page.get_by_text('上次扫码未完成，已重新准备二维码。是否跳过此平台登录？也可继续扫描下方二维码。',exact=True)).to_be_visible()
            page.set_viewport_size({'width':390,'height':850})
            box=page.get_by_role('dialog').bounding_box()
            assert box['x']>=0 and box['x']+box['width']<=391
            page.screenshot(path=str(ROOT/'research-login-mobile.png'))
            page.get_by_role('button',name='跳过此平台登录',exact=True).click()
            page.get_by_role('button',name='继续扫码',exact=True).click()
            expect(page.get_by_alt_text('小红书登录二维码')).to_be_visible()
            page.get_by_role('button',name='跳过此平台登录',exact=True).click()
            page.get_by_role('button',name='确认跳过此平台',exact=True).click()
            expect(page.get_by_role('dialog')).to_have_count(0)
            assert actions==[{'id':'qr1','action':'refresh'},{'id':'qr2','action':'skip'}]
            assert len(calls)==before
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
