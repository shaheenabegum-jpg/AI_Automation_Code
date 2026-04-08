"""
Standalone crawl worker — runs as a subprocess to avoid Windows event loop issues.
Reads JSON config from stdin, prints JSON result to stdout.

Usage: echo '{"url":"...","auth":{...}}' | python _crawl_worker.py
  OR:  python _crawl_worker.py <url> [timeout_ms]   (legacy, no auth)
"""
import sys
import json
import base64


def _perform_login(page, auth, timeout_ms):
    """
    Auto-login using project credentials.
    Supports Innoveo Skye login flow (Banorte/MGA):
      - Fill 'Enter username' with email
      - Fill 'Password here' with password
      - Click 'Log in' button
      - Wait for navigation
    """
    host = auth.get("pw_host", "")
    email = auth.get("pw_email", "")
    password = auth.get("pw_password", "")

    if not email or not password:
        return False, "Missing pw_email or pw_password"

    try:
        # Navigate to the app's base URL (login page)
        login_url = host if host else page.url
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2000)

        # Check if already logged in (no login form visible)
        username_field = page.get_by_placeholder("Enter username")
        if not username_field.is_visible(timeout=3000):
            return True, "Already logged in"

        # Fill credentials
        username_field.fill(email)
        page.get_by_placeholder("Password here").fill(password)
        page.get_by_role("button", name="Log in").click()

        # Wait for login to complete (redirect away from login page)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(3000)

        return True, "Login successful"

    except Exception as e:
        return False, f"Login failed: {repr(e)}"


def main():
    # Read config from stdin (JSON) or fallback to CLI args
    config = {}
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                config = json.loads(raw)
        except Exception:
            pass

    url = config.get("url") or (sys.argv[1] if len(sys.argv) > 1 else "")
    timeout_ms = config.get("timeout_ms") or (int(sys.argv[2]) if len(sys.argv) > 2 else 30000)
    auth = config.get("auth", {})  # { pw_host, pw_email, pw_password, pw_testuser }

    if not url:
        print(json.dumps({"error": "URL is required"}))
        return

    from playwright.sync_api import sync_playwright

    result = {
        "url": url, "title": "", "screenshot_b64": "",
        "elements": [], "element_count": 0,
        "accessibility_tree": "", "error": None,
        "login_status": None,
    }

    JS = """
    () => {
        const SELECTORS = 'button, a, input, select, textarea, ' +
            '[role="button"], [role="link"], [role="tab"], [role="menuitem"], ' +
            '[data-testid], [data-test-id], [onclick]';
        const MAX = 200;
        const els = Array.from(document.querySelectorAll(SELECTORS)).slice(0, MAX * 2);
        const results = [];
        for (const el of els) {
            if (results.length >= MAX) break;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) continue;
            const tag = el.tagName.toLowerCase();
            const testId = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || '';
            const id = el.id || '';
            const name = el.getAttribute('name') || '';
            const role = el.getAttribute('role') || '';
            const ariaLabel = el.getAttribute('aria-label') || '';
            const text = (el.innerText || el.textContent || '').trim().slice(0, 80);
            const placeholder = el.getAttribute('placeholder') || '';
            const href = el.getAttribute('href') || '';
            const type = el.getAttribute('type') || '';
            const classes = el.className ? String(el.className).slice(0, 100) : '';
            let selector = '';
            if (testId) selector = '[data-testid="' + testId + '"]';
            else if (id) selector = '#' + id;
            else if (ariaLabel) selector = '[aria-label="' + ariaLabel + '"]';
            else if (name) selector = '[name="' + name + '"]';
            else if (role && text) selector = '[role="' + role + '"]:has-text("' + text.slice(0, 40) + '")';
            else if (tag === 'a' && text) selector = 'a:has-text("' + text.slice(0, 40) + '")';
            else if (tag === 'button' && text) selector = 'button:has-text("' + text.slice(0, 40) + '")';
            else if (placeholder) selector = '[placeholder="' + placeholder + '"]';
            results.push({ tag, type, id, testId, name, role, ariaLabel, text, placeholder, href, classes, selector });
        }
        return results;
    }
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            page = context.new_page()

            # Auto-login if credentials provided
            if auth.get("pw_email") and auth.get("pw_password"):
                ok, msg = _perform_login(page, auth, timeout_ms)
                result["login_status"] = msg
                if not ok:
                    result["error"] = msg
                    print(json.dumps(result, default=str))
                    browser.close()
                    return

            # Navigate to target URL (after login if needed)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)

            result["title"] = page.title()
            result["url"] = page.url

            try:
                elements = page.evaluate(JS)
                result["elements"] = elements
                result["element_count"] = len(elements)
            except Exception:
                pass

            try:
                tree = page.accessibility.snapshot()
                if tree:
                    result["accessibility_tree"] = json.dumps(tree, indent=1, default=str)[:8000]
            except Exception:
                pass

            try:
                screenshot_bytes = page.screenshot(type="jpeg", quality=60)
                result["screenshot_b64"] = base64.b64encode(screenshot_bytes).decode()
            except Exception:
                pass

            browser.close()

    except Exception as e:
        result["error"] = f"Crawl failed: {repr(e)}"

    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
