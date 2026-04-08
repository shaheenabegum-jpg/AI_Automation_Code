"""Test if sync Playwright works inside a thread (simulating FastAPI context)."""
import asyncio
import threading
import json

async def main():
    result_holder = [{}]

    def worker():
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                title = page.title()
                elements = page.evaluate("""
                () => document.querySelectorAll('a, button, input').length
                """)
                browser.close()
                result_holder[0] = {"title": title, "elements": elements}
                print(f"[THREAD] Title: {title}, Elements: {elements}")
        except Exception as e:
            result_holder[0] = {"error": str(e)}
            print(f"[THREAD] Error: {e}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while t.is_alive():
        await asyncio.sleep(0.2)

    print(f"[MAIN] Result: {result_holder[0]}")

asyncio.run(main())
