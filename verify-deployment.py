"""Local deployment checks; never starts a platform crawl or accepts terms."""
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:8080'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / 'browsers')
os.environ['TEMP'] = os.environ['TMP'] = str(ROOT / 'tmp')


def get(path):
    with urlopen(BASE + path, timeout=10) as response:
        return response.read()


assert json.loads(get('/api/health'))['status'] == 'ok'
assert json.loads(get('/api/crawler/status'))['status'] == 'idle'
fixture = ROOT / 'app/data/_deployment-check/中文 样例.jsonl'
fixture.parent.mkdir(parents=True, exist_ok=True)
sample = [{'title': 'deployment check', 'id': 1}, {'title': '本地验证', 'id': 2}]
fixture.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in sample) + '\n', encoding='utf-8')
try:
    files = json.loads(get('/api/data/files'))['files']
    listed = next(item for item in files if item['name'] == fixture.name)
    assert listed['record_count'] == 2
    path = '/'.join(quote(segment, safe='') for segment in listed['path'].replace('\\', '/').split('/'))
    preview = json.loads(get('/api/data/files/' + path + '?limit=1'))
    assert preview == {'data': sample[:1], 'total': 2}
    assert get('/api/data/download/' + path) == fixture.read_bytes()
    print('PASS: JSONL list, bounded preview, Unicode filename and download')
finally:
    fixture.unlink()
    fixture.parent.rmdir()

with connect('ws://127.0.0.1:8080/api/ws/logs', open_timeout=10) as ws:
    ws.send('ping')
    assert ws.recv(timeout=5) == 'pong'
print('PASS: live log WebSocket heartbeat')

errors = []
with sync_playwright() as p:
    # Test full bundled Chromium, also used by non-headless crawler sessions.
    browser = p.chromium.launch(executable_path=p.chromium.executable_path, headless=True)
    page = browser.new_page(locale='zh-CN', viewport={'width': 1440, 'height': 1000})
    page.on('pageerror', lambda error: errors.append(str(error)))
    response = page.goto(BASE, wait_until='networkidle', timeout=30000)
    assert response and response.ok
    license_text = json.loads((ROOT / 'app/webui/src/i18n/locales/zh-CN/license.json').read_text(encoding='utf-8'))
    page.get_by_role('heading', name=license_text['title'], exact=True).wait_for()
    assert page.evaluate("localStorage.getItem('mediacrawler_license_accepted')") is None
    assert not errors, errors
    page.screenshot(path=str(ROOT / 'verification.png'), full_page=True)
    browser.close()
print('PASS: WebUI renders in Chromium without JavaScript errors; original license prompt preserved')
