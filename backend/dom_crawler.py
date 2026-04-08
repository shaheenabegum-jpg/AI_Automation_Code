"""
DOM Crawler — headless Playwright-based page crawler with Redis caching.

Crawls a URL, extracts interactive elements + accessibility tree + screenshot.
Results are cached in Redis (1hr TTL) to avoid re-crawling.

Uses a subprocess worker (_crawl_worker.py) to avoid Windows SelectorEventLoop
limitations with Playwright's internal event loop.

Usage:
    result = await crawl_page("https://example.com")
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import redis
from config import settings

logger = logging.getLogger(__name__)

_redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
CACHE_TTL = 3600  # 1 hour

# Path to the standalone crawl worker script
_WORKER_SCRIPT = str(Path(__file__).resolve().parent / "_crawl_worker.py")
_PYTHON = sys.executable  # Same Python interpreter as the backend


async def crawl_page(
    url: str,
    timeout_ms: int = 30000,
    auth: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Crawl a URL and extract interactive elements, accessibility tree, and screenshot.
    Cached in Redis for 1 hour (screenshot excluded from cache).

    Args:
        auth: Optional dict with pw_host, pw_email, pw_password, pw_testuser
              for auto-login before crawling protected pages.
    """
    url = url.strip()
    if not url:
        return {"error": "URL is required"}

    # Skip cache if auth is provided (authenticated pages vary by session)
    if not auth:
        cache_key = "dom_crawl:" + hashlib.sha256(url.encode()).hexdigest()
        try:
            cached = _redis.get(cache_key)
            if cached:
                logger.info("DOM cache hit for %s", url)
                result = json.loads(cached)
                result["screenshot_b64"] = ""
                return result
        except Exception:
            pass
    else:
        cache_key = None

    return await _subprocess_crawl(url, timeout_ms, cache_key, auth)


async def _subprocess_crawl(
    url: str, timeout_ms: int, cache_key: str | None,
    auth: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the crawl worker as a subprocess and parse its JSON output."""
    import threading

    # Build config JSON for stdin (supports auth)
    config = {"url": url, "timeout_ms": timeout_ms}
    if auth:
        config["auth"] = auth
    config_json = json.dumps(config)

    cmd = [_PYTHON, _WORKER_SCRIPT]
    logger.info("Spawning crawl worker (auth=%s): %s", bool(auth), _WORKER_SCRIPT)

    result_holder: list[dict] = [{"error": "Crawl subprocess did not complete"}]

    def _run():
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                input=config_json,
                timeout=timeout_ms // 1000 + 30,  # extra buffer for login
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()[:200]
                result_holder[0] = {"error": f"Worker exit {proc.returncode}: {stderr}"}
                return
            result_holder[0] = json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            result_holder[0] = {"error": f"Crawl timed out after {timeout_ms}ms"}
        except Exception as e:
            result_holder[0] = {"error": f"Subprocess error: {repr(e)}"}

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    while t.is_alive():
        await asyncio.sleep(0.3)
    t.join(timeout=1)

    result = result_holder[0]
    if result.get("error"):
        return result

    # Cache in Redis (only for non-authenticated crawls)
    if cache_key:
        try:
            cache_data = {k: v for k, v in result.items()
                         if k not in ("screenshot_b64", "login_status")}
            _redis.setex(cache_key, CACHE_TTL, json.dumps(cache_data, default=str))
        except Exception as e:
            logger.warning("Redis cache write failed: %s", e)

    return result
