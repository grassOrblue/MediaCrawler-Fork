"""Check small-window scrolling without accepting the project's terms."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parent
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(root / 'browsers')
os.environ['TEMP'] = os.environ['TMP'] = str(root / 'tmp')
config = json.loads((root / 'app/webui/src/i18n/locales/zh-CN/config.json').read_text(encoding='utf-8'))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width, height in [(1440, 900), (1366, 768), (1024, 600), (768, 500), (390, 700), (600, 320)]:
        page = browser.new_page(viewport={'width': width, 'height': height}, locale='zh-CN')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8080', wait_until='networkidle')
        heading = page.get_by_role('heading', name='使用须知', exact=True)
        overlay = heading.locator('xpath=ancestor::div[contains(@class,"fixed")]')
        heading.scroll_into_view_if_needed()
        assert heading.bounding_box()['y'] >= 0, (width, height, 'license title clipped')
        confirm = page.get_by_role('button', name='我已知晓并同意上述条款', exact=True)
        confirm.scroll_into_view_if_needed()
        rect = confirm.bounding_box()
        assert rect['y'] >= 0 and rect['y'] + rect['height'] <= height + 1
        # Hide only this ephemeral browser's modal to inspect the already mounted
        # application underneath. Never click acceptance or modify localStorage.
        overlay.evaluate('e => e.style.display = "none"')
        scroll = page.locator('#root > div > div.overflow-y-auto:not(.fixed)')
        assert scroll.evaluate('e => e.scrollHeight >= e.clientHeight')
        start = page.get_by_role('button', name=config['button']['initiateScan'], exact=True)
        start.scroll_into_view_if_needed()
        assert start.evaluate('e => { const a=e.getBoundingClientRect(); const b=e.closest(".overflow-y-auto").getBoundingClientRect(); return a.top>=b.top && a.bottom<=b.bottom+1 }'), (width, height, 'start button unreachable')
        logs = page.get_by_text('系统控制台', exact=True)
        logs.scroll_into_view_if_needed()
        assert page.locator('main').bounding_box()['height'] >= 320
        assert page.locator('.terminal-scroll').bounding_box()['height'] >= 100
        assert page.get_by_role('banner').evaluate('e => e.scrollWidth <= e.clientWidth+1')
        assert page.locator('footer').count() == 0
        assert page.evaluate("localStorage.getItem('mediacrawler_license_accepted')") is None
        assert not errors, errors
        if (width, height) in [(1024, 600), (390, 700)]:
            page.screenshot(path=str(root / f'layout-fixed-{width}x{height}.png'))
        print(f'PASS {width}x{height}: modal, config button and console reachable; logs retain their own scroll area')
        page.close()
    browser.close()
