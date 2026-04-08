import json
from playwright.sync_api import sync_playwright

url = "https://www.google.com"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 720}, ignore_https_errors=True)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    print("Title:", page.title())
    print("URL:", page.url)

    elements = page.evaluate("""
    () => {
        const els = document.querySelectorAll('button, a, input, select, textarea, [role="button"], [data-testid]');
        return Array.from(els).slice(0, 10).map(el => ({
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || '').trim().slice(0, 40),
            id: el.id || '',
            name: el.getAttribute('name') || '',
        }));
    }
    """)
    print(f"Elements found: {len(elements)}")
    for e in elements[:5]:
        print(f"  {e['tag']} id={e['id']} name={e['name']} text={e['text'][:30]}")

    browser.close()
