from playwright.sync_api import sync_playwright

p = sync_playwright().start()
b = p.chromium.launch(headless=True)
print("Browser OK:", b.version)
b.close()
p.stop()
