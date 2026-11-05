"""Exercise platform browser launchers and failure status without scraping."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / 'browsers')
os.environ['MPLCONFIGDIR'] = str(ROOT / 'cache/matplotlib')
os.environ['TEMP'] = os.environ['TMP'] = str(ROOT / 'tmp')
sys.path.insert(0, str(APP))
os.chdir(APP)
from playwright.async_api import async_playwright
from main import CrawlerFactory
from api.services.crawler_manager import CrawlerManager
import config


async def check_exit_status():
    for exit_code, expected in [(1, 'error'), (0, 'idle')]:
        manager = CrawlerManager()
        manager.status = 'running'
        manager.process = subprocess.Popen(
            [sys.executable, '-c', f'print("local startup test"); raise SystemExit({exit_code})'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        )
        await asyncio.wait_for(manager._read_output(), timeout=10)
        assert manager.get_status()['status'] == expected
        assert bool(manager.get_status()['error_message']) == (exit_code != 0)
        manager.process.stdout.close()
    print('PASS: failed subprocess reports error; successful subprocess returns idle', flush=True)


async def check_browsers():
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='platform-browser-', dir=ROOT / 'tmp') as temp:
        os.chdir(temp)
        try:
            async with async_playwright() as p:
                for platform in CrawlerFactory.CRAWLERS:
                    config.PLATFORM = platform
                    config.SAVE_LOGIN_STATE = True
                    config.USER_DATA_DIR = '%s_test_profile'
                    crawler = CrawlerFactory.create_crawler(platform)
                    context = await crawler.launch_browser(p.chromium, None, None, headless=True)
                    page = await context.new_page()
                    await page.set_content('<h1>local browser launch passed</h1>')
                    assert await page.locator('h1').inner_text() == 'local browser launch passed'
                    await context.close()
                    print(f'PASS: {platform} persistent browser launcher', flush=True)
                # Exercise the full bundled executable with a persistent profile,
                # matching the executable used when the user requests a visible window.
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(temp) / 'full-chromium-profile'),
                    executable_path=p.chromium.executable_path, headless=True,
                )
                await context.close()
                config.SAVE_LOGIN_STATE = False
                crawler = CrawlerFactory.create_crawler('bili')
                context = await crawler.launch_browser(p.chromium, None, None, headless=True)
                await context.browser.close()
                print('PASS: full bundled Chromium profile and Bilibili nonpersistent launcher', flush=True)
        finally:
            os.chdir(previous)


async def main():
    await check_exit_status()
    await check_browsers()


asyncio.run(main())
