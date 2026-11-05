"""Read-only UI acceptance: new-window entry, library filters and details."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parent
os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(ROOT/'browsers')
os.environ['TEMP']=os.environ['TMP']=str(ROOT/'tmp')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000},locale='zh-CN')
    home=context.new_page()
    errors=[]
    home.on('pageerror',lambda error:errors.append(str(error)))
    home.goto('http://127.0.0.1:8080',wait_until='networkidle')
    home.get_by_role('heading',name='使用须知',exact=True).evaluate('e=>e.closest(".fixed").style.display="none"')
    expect(home.locator('footer')).to_have_count(0)
    expect(home.get_by_alt_text('程序员阿江-Relakkes',exact=True)).to_have_count(0)
    expect(home.get_by_role('dialog')).to_have_count(0)
    with home.expect_popup() as opened:
        home.get_by_role('link',name='打开数据资料库',exact=True).click()
    library=opened.value
    library.on('pageerror',lambda error:errors.append(str(error)))
    library.wait_for_url('http://127.0.0.1:8081/')
    library.wait_for_load_state('networkidle')
    expect(library.get_by_role('heading',name='内容一览',exact=True)).to_be_visible()
    expect(library.locator('.lib-card')).to_have_count(24)
    photo=library.locator('.lib-cover img').first
    expect(photo).to_be_visible()
    assert photo.evaluate('e=>e.complete&&e.naturalWidth>0')
    library.screenshot(path=str(ROOT/'library-desktop.png'),full_page=True)
    first=library.locator('.lib-title').first.inner_text()
    library.get_by_role('button',name='下一页',exact=True).click()
    expect(library.locator('.lib-pagination')).to_contain_text('第 2 页')
    expect(library.locator('.lib-title').first).not_to_have_text(first)
    library.get_by_label('包含关键词',exact=True).fill('编程')
    library.get_by_role('button',name='应用筛选',exact=True).click()
    expect(library.locator('.lib-pagination')).to_contain_text('第 1 页')
    expect(library.locator('.lib-card').first).to_be_visible()
    library.get_by_role('button',name='查看详情',exact=False).filter(has=library.locator('svg')).first.click()
    expect(library.locator('.lib-detail-content')).to_be_visible()
    expect(library.get_by_role('dialog')).to_have_count(0)
    expect(library.get_by_role('heading',name='我的整理',exact=True)).to_be_visible()
    library.screenshot(path=str(ROOT/'library-detail.png'),full_page=True)
    library.get_by_role('button',name='返回筛选结果',exact=True).click()
    expect(library.get_by_label('包含关键词',exact=True)).to_have_value('编程')
    for width,height in [(1024,768),(390,800)]:
        library.set_viewport_size({'width':width,'height':height})
        assert library.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(width,'horizontal overflow')
        library.get_by_role('button',name='应用筛选',exact=True).scroll_into_view_if_needed()
        library.locator('.lib-title').first.scroll_into_view_if_needed()
        if width==390:
            library.screenshot(path=str(ROOT/'library-mobile.png'))
    assert not errors,errors
    assert home.evaluate("localStorage.getItem('mediacrawler_license_accepted')") is None
    browser.close()
print('PASS: prominent new-window entry, removed author bar, 24-card pagination, local thumbnails, Chinese filter, full-page detail, filter retention and responsive layout')
