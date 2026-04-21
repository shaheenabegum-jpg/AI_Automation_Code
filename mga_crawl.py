"""
MGA Application Crawler — runs on Windows (Anaconda Python) with VPN access.
Logs in, navigates all visible pages, extracts DOM elements + screenshot,
and POSTs each snapshot to the platform's /api/import-snapshot endpoint.

Usage:  python mga_crawl.py
"""

import asyncio
import base64
import json
import re
import sys
import urllib.request
import urllib.parse

# Force UTF-8 output on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MGA_URL      = "https://skye1.dev.mga.innoveo-skye.net"
EMAIL        = "yash.bodhale+MGAUA@tinubu.com"
PASSWORD     = "MGA@123"
PROJECT_ID   = "f376a260-cdeb-4366-90d9-59bf5002c403"
API_BASE     = "http://localhost:5174"

# ---------------------------------------------------------------------------

def post_snapshot(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{API_BASE}/api/import-snapshot",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


async def extract_elements(page):
    """Extract interactive + visible elements from the current page."""
    return await page.evaluate("""() => {
        const seen = new Set();
        const elements = [];
        const selectors = [
            'a[href]', 'button', 'input', 'select', 'textarea',
            'label', '[role="button"]', '[role="link"]', '[role="menuitem"]',
            '[role="tab"]', '[role="navigation"]', 'nav', 'h1', 'h2', 'h3',
            '[data-testid]', '[id]', '[name]'
        ];
        for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const key = el.tagName + (el.id || el.name || el.textContent?.trim()?.slice(0,30));
                if (seen.has(key)) continue;
                seen.add(key);
                elements.push({
                    tag:         el.tagName.toLowerCase(),
                    type:        el.type || null,
                    id:          el.id   || null,
                    name:        el.name || null,
                    text:        el.textContent?.trim()?.slice(0, 100) || null,
                    placeholder: el.placeholder || null,
                    href:        el.href || null,
                    role:        el.getAttribute('role') || null,
                    testid:      el.getAttribute('data-testid') || null,
                    class:       el.className?.slice?.(0, 80) || null,
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height),
                });
                if (elements.length >= 300) break;
            }
            if (elements.length >= 300) break;
        }
        return elements;
    }""")


async def crawl():
    from playwright.async_api import async_playwright

    print(f"\n{'='*60}")
    print("MGA Crawler starting …")
    print(f"Target : {MGA_URL}")
    print(f"{'='*60}\n")

    snapshots_saved = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)   # headed so you can watch
        ctx     = await browser.new_context(viewport={"width": 1400, "height": 900})
        page    = await ctx.new_page()

        # ── 1. Login ──────────────────────────────────────────────────────────
        print(">>  Navigating to login page …")
        await page.goto(MGA_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        print(">>  Filling credentials …")
        # Try common selectors for username/email
        for sel in ['input[placeholder*="username" i]', 'input[placeholder*="email" i]',
                    'input[type="email"]', 'input[name="email"]', 'input[name="username"]',
                    'input[id*="user" i]', 'input[id*="email" i]']:
            try:
                await page.fill(sel, EMAIL, timeout=3000)
                print(f"   [+] Filled email via: {sel}")
                break
            except:
                pass

        for sel in ['input[type="password"]', 'input[placeholder*="password" i]',
                    'input[name="password"]', 'input[id*="pass" i]']:
            try:
                await page.fill(sel, PASSWORD, timeout=3000)
                print(f"   [+] Filled password via: {sel}")
                break
            except:
                pass

        # Click login/sign-in button
        for sel in ['button[type="submit"]', 'button:has-text("Log in")',
                    'button:has-text("Login")', 'button:has-text("Sign in")',
                    'input[type="submit"]']:
            try:
                await page.click(sel, timeout=3000)
                print(f"   [+] Clicked login via: {sel}")
                break
            except:
                pass

        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(3000)
        print(f"   [+] Logged in — current URL: {page.url}\n")

        # ── 2. Snapshot helper ────────────────────────────────────────────────
        async def snapshot_page(label: str):
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(1500)

            url   = page.url
            title = await page.title()
            print(f"   [SNAP]  Snapping: [{label}] {title} — {url}")

            elements = await extract_elements(page)
            shot_bytes = await page.screenshot(type="jpeg", quality=70, full_page=False)
            screenshot_b64 = base64.b64encode(shot_bytes).decode()

            payload = {
                "url":              url,
                "title":            title,
                "elements":         elements,
                "screenshot_b64":   screenshot_b64,
                "accessibility_tree": "",
                "project_id":       PROJECT_ID,
            }
            try:
                result = post_snapshot(payload)
                print(f"      [OK] Saved snapshot {result['snapshot_id']} "
                      f"({result['element_count']} elements)")
                snapshots_saved.append({"label": label, "url": url, "title": title,
                                        "snapshot_id": result["snapshot_id"],
                                        "elements": result["element_count"]})
            except Exception as e:
                print(f"      [ERR] Failed to save snapshot: {e}")

        # ── 3. Snapshot login / dashboard ─────────────────────────────────────
        await snapshot_page("Post-Login / Dashboard")

        # ── 4. Discover nav links ─────────────────────────────────────────────
        print("\n>>  Discovering navigation links …")
        nav_links = await page.evaluate(f"""() => {{
            const base = '{MGA_URL}';
            const links = new Set();
            for (const a of document.querySelectorAll('a[href]')) {{
                const href = a.href;
                if (href && href.startsWith(base) && !href.includes('#') && href !== base + '/') {{
                    links.add(href);
                }}
            }}
            // Also look inside nav / sidebar
            for (const el of document.querySelectorAll('nav a, aside a, [role="navigation"] a, [class*="sidebar"] a, [class*="menu"] a')) {{
                if (el.href && el.href.startsWith(base)) links.add(el.href);
            }}
            return Array.from(links).slice(0, 30);
        }}""")

        print(f"   Found {len(nav_links)} navigation links\n")

        visited = {page.url}

        # ── 5. Visit each nav link ────────────────────────────────────────────
        for href in nav_links:
            if href in visited:
                continue
            visited.add(href)
            label = re.sub(r'https?://[^/]+', '', href).strip('/') or 'home'
            print(f"\n>>  Navigating to: {href}")
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                await snapshot_page(label)
            except Exception as e:
                print(f"   [WARN]  Skipped {href}: {e}")

        await browser.close()

    # ── 6. Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"[OK]  Crawl complete — {len(snapshots_saved)} snapshots saved\n")
    for s in snapshots_saved:
        print(f"   [{s['label']}] {s['title']}")
        print(f"      URL: {s['url']}")
        print(f"      Snapshot ID: {s['snapshot_id']}  |  Elements: {s['elements']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(crawl())
