"""
Framework Loader — fetches relevant TypeScript files from the private
QA_Automation_Banorte GitHub repo and caches them in Redis.

The actual framework lives inside the skye-e2e-tests/ subdirectory of the repo.

Files fetched:
  - skye-e2e-tests/playwright.config.ts
  - All files under skye-e2e-tests/fixtures/  (Fixtures.ts, BanorteCommandsFixture.ts…)
  - All files under skye-e2e-tests/pages/     (PetsPage, MainPage, BasePage…)
  - All files under skye-e2e-tests/custom/    (SkyeAttributeCommands, BanorteCommands…)
  - All files under skye-e2e-tests/utils/     (if present)

The combined text (capped at ~45 000 chars to fit prompt budget) is stored
in Redis with a 1-hour TTL so repeated generation calls don't hit GitHub.
"""
import hashlib
import logging
import os
from typing import Optional
import redis
from github import Github, UnknownObjectException
from config import settings

logger = logging.getLogger(__name__)

# ── Redis client (sync — used only for caching, not on hot path) ─────────────────
_redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
CACHE_KEY = "framework_context"
HASH_KEY = "framework_context_hash"
CACHE_TTL = 3600  # 1 hour

# ── Directories to fetch from the framework repo ─────────────────────────────────
# The actual framework lives inside the skye-e2e-tests/ subdirectory of the repo.
FETCH_PATHS = [
    "skye-e2e-tests/playwright.config.ts",  # Playwright config (projects, reporters)
    "skye-e2e-tests/fixtures",              # Fixtures.ts, BanorteCommandsFixture.ts, etc.
    "skye-e2e-tests/pages",                 # All Page Object Models (PetsPage, MainPage…)
    "skye-e2e-tests/custom",                # SkyeAttributeCommands.ts, BanorteCommands.ts
    "skye-e2e-tests/utils",                 # Utility helpers (if present)
]

MAX_CONTEXT_CHARS = 45_000            # ~14 K tokens — leaves room for prompt


def get_framework_context(force_refresh: bool = False) -> tuple[str, str]:
    """
    Returns (context_text, sha256_hash).
    Uses Redis cache unless force_refresh=True.
    """
    if not force_refresh:
        cached = _redis.get(CACHE_KEY)
        cached_hash = _redis.get(HASH_KEY) or ""
        if cached:
            logger.info("Framework context served from Redis cache")
            return cached, cached_hash

    logger.info("Fetching framework context from GitHub…")
    context = _fetch_from_github()
    ctx_hash = hashlib.sha256(context.encode()).hexdigest()

    _redis.setex(CACHE_KEY, CACHE_TTL, context)
    _redis.setex(HASH_KEY, CACHE_TTL, ctx_hash)
    logger.info("Framework context cached (%d chars)", len(context))
    return context, ctx_hash


def _fetch_from_github() -> str:
    g = Github(settings.GITHUB_TOKEN)
    repo = g.get_repo(settings.GITHUB_FRAMEWORK_REPO)
    parts: list[str] = []

    for path in FETCH_PATHS:
        try:
            contents = repo.get_contents(path)
        except UnknownObjectException:
            logger.debug("Path not found in repo: %s", path)
            continue

        if isinstance(contents, list):
            # Directory → iterate files
            for item in contents:
                if item.name.endswith((".ts", ".json")):
                    _append_file(parts, item)
        else:
            # Single file
            _append_file(parts, contents)

        if sum(len(p) for p in parts) >= MAX_CONTEXT_CHARS:
            logger.info("Reached MAX_CONTEXT_CHARS, stopping fetch")
            break

    combined = "\n\n".join(parts)
    return combined[:MAX_CONTEXT_CHARS]


def _append_file(parts: list[str], file_content) -> None:
    try:
        text = file_content.decoded_content.decode("utf-8", errors="replace")
        parts.append(f"// ═══ FILE: {file_content.path} ═══\n{text}")
    except Exception as e:
        logger.warning("Could not decode %s: %s", file_content.path, e)


def invalidate_cache() -> None:
    _redis.delete(CACHE_KEY)
    _redis.delete(HASH_KEY)
    logger.info("Framework context cache invalidated")
