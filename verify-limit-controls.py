"""Verify form payloads with the start endpoint intercepted locally."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / 'browsers')
os.environ['TEMP'] = os.environ['TMP'] = str(ROOT / 'tmp')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1366, 'height': 768}, locale='zh-CN')
    submissions = []

    def intercept(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=200, json={'status': 'ok'})

    page.route('**/api/crawler/start', intercept)
    page.goto('http://127.0.0.1:8080', wait_until='networkidle')
    page.get_by_role('heading', name='使用须知', exact=True).evaluate('e=>e.closest(".fixed").style.display="none"')
    page.get_by_role('combobox', name='平台', exact=True).click()
    page.get_by_role('option', name='Xiaohongshu', exact=True).click()
    notes = page.get_by_role('spinbutton', name='帖子数量上限（每个关键词／创作者）', exact=True)
    comments = page.get_by_role('spinbutton', name='每帖一级评论上限', exact=True)
    notes.fill('100001')
    comments.fill('35')
    page.get_by_role('spinbutton', name='请求间隔（秒）', exact=True).fill('8')
    start = page.get_by_role('button', name='开始爬虫', exact=True)
    start.click()
    expect(start).to_be_enabled(timeout=6000)
    assert submissions[-1]['max_notes_count'] == 100001
    assert submissions[-1]['max_comments_count'] == 35
    assert submissions[-1]['request_interval'] == 8
    assert submissions[-1]['rate_mode'] == 'auto'
    assert submissions[-1]['stop_condition'] == 'count'
    page.get_by_role('combobox', name='停止条件', exact=True).click()
    page.get_by_role('option', name='没有更多帖子时停止', exact=True).click()
    expect(notes).to_have_count(0)
    start.click()
    expect(start).to_be_enabled(timeout=6000)
    assert submissions[-1]['stop_condition'] == 'exhausted'
    assert submissions[-1]['platform'] == 'xhs'
    page.get_by_role('combobox', name='平台', exact=True).click()
    page.get_by_role('option', name='Bilibili', exact=True).click()
    expect(notes).to_be_visible()
    page.get_by_role('combobox', name='停止条件', exact=True).click()
    expect(page.get_by_role('option', name='没有更多帖子时停止', exact=True)).to_have_count(0)
    assert page.evaluate("localStorage.getItem('mediacrawler_license_accepted')") is None
    browser.close()
print('PASS: quantity/comment values submitted, exhaustion option scoped to XHS search; all start requests intercepted, no real crawl')
