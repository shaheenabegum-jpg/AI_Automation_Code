"""
GitHub Actions Runner
=====================
Executes Playwright tests via GitHub Actions instead of running locally.

Flow per test run:
  1. Ensure 'ai-tests-staging' branch exists (created from main if needed)
  2. Commit the .spec.ts file to staging branch via GitHub Contents API
  3. Discover the existing Playwright/test workflow in the repo
  4. Trigger workflow_dispatch on the staging branch
  5. Poll for run completion every 10s, publishing status to Redis → WebSocket
  6. If PASSED → commit the file to 'ai-generated-tests' branch (permanent)
  7. Return (exit_code, github_run_url)
"""
import asyncio
import base64
import logging
import time
from datetime import datetime, timezone

import httpx
import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

STAGING_BRANCH = "ai-tests-staging"
RESULTS_BRANCH = "ai-generated-tests"
API_BASE       = "https://api.github.com"


# ── GitHub API helpers ──────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    return settings.GITHUB_FRAMEWORK_REPO   # e.g. RajasekharPlay/QA_Automation_Banorte


async def _get_default_sha(client: httpx.AsyncClient) -> str:
    """Return HEAD SHA of main (falls back to master)."""
    for branch in ("main", "master"):
        resp = await client.get(
            f"{API_BASE}/repos/{_repo()}/git/ref/heads/{branch}",
            headers=_headers(),
        )
        if resp.status_code == 200:
            return resp.json()["object"]["sha"]
    raise RuntimeError(f"Could not find main/master branch in {_repo()}")


async def _ensure_branch(client: httpx.AsyncClient, branch: str) -> None:
    """Create branch from main if it does not exist."""
    resp = await client.get(
        f"{API_BASE}/repos/{_repo()}/git/ref/heads/{branch}",
        headers=_headers(),
    )
    if resp.status_code == 200:
        return  # already exists
    sha = await _get_default_sha(client)
    create = await client.post(
        f"{API_BASE}/repos/{_repo()}/git/refs",
        headers=_headers(),
        json={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    create.raise_for_status()
    logger.info("Created branch '%s' from %s", branch, sha[:8])


async def _get_file_sha(
    client: httpx.AsyncClient, branch: str, path: str
) -> str | None:
    """Return existing file blob SHA (required for updates)."""
    resp = await client.get(
        f"{API_BASE}/repos/{_repo()}/contents/{path}",
        headers=_headers(),
        params={"ref": branch},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("sha")


async def _commit_file(
    client: httpx.AsyncClient,
    branch: str,
    file_path: str,
    content: str,
    message: str,
) -> str:
    """Create or update a file in the given branch. Returns commit SHA."""
    existing_sha = await _get_file_sha(client, branch, file_path)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    payload: dict = {"message": message, "content": encoded, "branch": branch}
    if existing_sha:
        payload["sha"] = existing_sha

    resp = await client.put(
        f"{API_BASE}/repos/{_repo()}/contents/{file_path}",
        headers=_headers(),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()["commit"]["sha"]


async def _discover_workflow(client: httpx.AsyncClient) -> tuple[int, str]:
    """Find the Playwright/test workflow. Returns (workflow_id, workflow_name)."""
    resp = await client.get(
        f"{API_BASE}/repos/{_repo()}/actions/workflows",
        headers=_headers(),
    )
    resp.raise_for_status()
    workflows = resp.json().get("workflows", [])

    keywords = ["playwright", "test", "e2e", "run", "ci"]
    for kw in keywords:
        for wf in workflows:
            if kw in wf.get("name", "").lower() or kw in wf.get("path", "").lower():
                logger.info("Discovered workflow: %s (id=%s)", wf["name"], wf["id"])
                return wf["id"], wf["name"]

    active = [w for w in workflows if w.get("state") == "active"]
    if not active:
        raise RuntimeError(f"No active workflows found in {_repo()}")
    return active[0]["id"], active[0]["name"]


async def _trigger_workflow(
    client: httpx.AsyncClient,
    workflow_id: int,
    branch: str,
    inputs: dict,
) -> None:
    """POST workflow_dispatch. Retries without inputs if 422."""
    resp = await client.post(
        f"{API_BASE}/repos/{_repo()}/actions/workflows/{workflow_id}/dispatches",
        headers=_headers(),
        json={"ref": branch, "inputs": inputs},
    )
    if resp.status_code == 422:
        # Workflow may not declare inputs — trigger without them
        logger.warning("workflow_dispatch returned 422 — retrying without inputs")
        resp = await client.post(
            f"{API_BASE}/repos/{_repo()}/actions/workflows/{workflow_id}/dispatches",
            headers=_headers(),
            json={"ref": branch},
        )
    resp.raise_for_status()


def _iso_to_ts(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


async def _wait_for_run(
    client: httpx.AsyncClient,
    workflow_id: int,
    branch: str,
    triggered_after: float,
    pub,           # async callable(msg: str) — publishes to pub/sub AND history list
    timeout_s: int = 900,
) -> tuple[str, str]:
    """
    Poll GitHub API for the latest run triggered after `triggered_after`.
    Publishes progress updates via pub() so messages reach BOTH Redis pub/sub
    (WebSocket) AND the history list (HTTP fallback polling).
    Returns (conclusion, html_url).
    """
    deadline = time.time() + timeout_s
    poll_run_id: int | None = None

    # Wait up to 60s for the run to appear in the API (check every 3s = 20 attempts)
    await pub("⏳ Waiting for GitHub Actions runner to pick up the job…")
    for _ in range(20):
        await asyncio.sleep(3)
        resp = await client.get(
            f"{API_BASE}/repos/{_repo()}/actions/workflows/{workflow_id}/runs",
            headers=_headers(),
            params={"branch": branch, "per_page": 10},
        )
        if resp.status_code != 200:
            continue
        runs = resp.json().get("workflow_runs", [])
        new_runs = [
            r2 for r2 in runs
            if _iso_to_ts(r2.get("created_at", "0")) >= (triggered_after - 10)
        ]
        if new_runs:
            poll_run_id = new_runs[0]["id"]
            break

    if not poll_run_id:
        await pub("⚠ Could not detect GitHub Actions run — check Actions tab manually.")
        return "unknown", f"https://github.com/{_repo()}/actions"

    html_url = f"https://github.com/{_repo()}/actions/runs/{poll_run_id}"
    await pub(f"🔗 GitHub Actions run: {html_url}")

    # Poll every 5s until complete or timeout (was 10s — reduced for faster UI feedback)
    last_status = ""
    while time.time() < deadline:
        await asyncio.sleep(5)
        resp = await client.get(
            f"{API_BASE}/repos/{_repo()}/actions/runs/{poll_run_id}",
            headers=_headers(),
        )
        if resp.status_code != 200:
            continue
        data       = resp.json()
        status     = data.get("status", "unknown")
        conclusion = data.get("conclusion")
        elapsed    = int(time.time() - triggered_after)

        # Only log a status line when something changes (avoids log spam)
        status_line = f"{status}" + (f" | {conclusion}" if conclusion else "")
        if status_line != last_status:
            await pub(f"⏳ GHA status: {status_line} | elapsed={elapsed}s")
            last_status = status_line
        logger.info("GHA poll run=%s: %s | %s", poll_run_id, status, conclusion)

        if status == "completed":
            if conclusion == "success":
                await pub("✅ GitHub Actions PASSED")
            else:
                await pub(f"❌ GitHub Actions FAILED (conclusion={conclusion})")
            await pub(f"🔗 Full logs: {html_url}")
            return conclusion or "unknown", html_url

    await pub("⏰ Timed out waiting for GitHub Actions run")
    return "timed_out", html_url


# ── Public API ──────────────────────────────────────────────────────────────────

async def run_test_via_github_actions(
    run_id: str,
    script_code: str,
    spec_filename: str,   # e.g. "RB001_RB_Pets_Landing_Page.spec.ts"
    browser: str,
    environment: str,
    device: str,
) -> tuple[int, str, str | None]:
    """
    Orchestrates the full GitHub Actions test execution flow.
    Returns (exit_code, github_run_url, committed_branch | None).
      - exit_code 0 = passed, 1 = failed
      - committed_branch is RESULTS_BRANCH if test passed, else None
    """
    r = aioredis.from_url(settings.REDIS_URL)
    channel = f"run:{run_id}:logs"
    history_key = f"run:{run_id}:log_history"

    async def pub(msg: str) -> None:
        """Publish to Redis pub/sub AND append to history list (for late subscribers)."""
        await r.publish(channel, msg)
        await r.rpush(history_key, msg)
        await r.expire(history_key, 86400)   # 24 h TTL, refreshed on each message

    # Brief delay so the WebSocket client has time to connect and subscribe to Redis
    # before we start publishing.  Without this the first N messages are silently lost.
    await asyncio.sleep(2)

    file_repo_path = f"skye-e2e-tests/tests/generated/{spec_filename}"

    await pub(f"▶ Starting GitHub Actions run [{run_id}]")
    await pub(f"  Repo    : {_repo()}")
    await pub(f"  File    : {file_repo_path}")
    await pub(f"  Env     : {environment.upper()} | {browser} | {device}")
    await pub("─" * 60)

    github_run_url: str = ""
    committed_branch: str | None = None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:

            # 1. Ensure staging branch exists
            await pub(f"📂 Preparing staging branch '{STAGING_BRANCH}'…")
            await _ensure_branch(client, STAGING_BRANCH)

            # 2. Commit test file to staging branch
            await pub(f"📝 Committing {spec_filename} to '{STAGING_BRANCH}'…")
            await _commit_file(
                client,
                branch=STAGING_BRANCH,
                file_path=file_repo_path,
                content=script_code,
                message=f"ci: stage {spec_filename} for test run [{run_id[:8]}]",
            )
            await pub(f"✓ Test file staged at {file_repo_path}")

            # 3. Discover existing workflow
            await pub("🔍 Discovering existing Playwright workflow…")
            workflow_id, workflow_name = await _discover_workflow(client)
            await pub(f"✓ Using workflow: '{workflow_name}' (id={workflow_id})")

            # 4. Trigger workflow_dispatch
            triggered_at = time.time()
            inputs = {
                "test_file":   file_repo_path,
                "browser":     browser,
                "environment": environment,
            }
            # ── IMPORTANT: trigger on 'main', NOT on STAGING_BRANCH ───────────
            # workflow_dispatch runs the YAML file from the ref you specify.
            # ai-tests-staging has an old version of playwright.yml that doesn't
            # sync playwright.config.ts from main → ai-* projects missing.
            # The fixed playwright.yml is on main.  The YAML itself hardcodes
            # "checkout ai-tests-staging" so the spec file is always found there.
            TRIGGER_BRANCH = "main"
            await pub(f"🚀 Triggering workflow on '{TRIGGER_BRANCH}' (reads spec from '{STAGING_BRANCH}')…")
            await _trigger_workflow(client, workflow_id, TRIGGER_BRANCH, inputs)
            await pub("✓ Workflow triggered — polling for completion…")

            # 5. Poll until done — filter by TRIGGER_BRANCH since dispatch ran on it
            conclusion, github_run_url = await _wait_for_run(
                client, workflow_id, TRIGGER_BRANCH, triggered_at, pub
            )

            exit_code = 0 if conclusion == "success" else 1

            # 6. If passed → commit to results branch
            if exit_code == 0:
                await pub(f"📦 Committing script to '{RESULTS_BRANCH}' branch…")
                try:
                    await _ensure_branch(client, RESULTS_BRANCH)
                    commit_sha = await _commit_file(
                        client,
                        branch=RESULTS_BRANCH,
                        file_path=file_repo_path,
                        content=script_code,
                        message=f"feat: add AI-generated test {spec_filename} ✅",
                    )
                    committed_branch = RESULTS_BRANCH
                    await pub(f"✅ Script committed to '{RESULTS_BRANCH}' (sha: {commit_sha[:8]})")
                    await pub(
                        f"🌿 View: https://github.com/{_repo()}/blob/{RESULTS_BRANCH}/{file_repo_path}"
                    )
                except Exception as e:
                    await pub(f"⚠ Could not commit to '{RESULTS_BRANCH}': {e}")
            else:
                await pub(f"⚠ Test FAILED — script NOT committed to '{RESULTS_BRANCH}'")
                await pub(f"   File remains in '{STAGING_BRANCH}' for debugging")

    except Exception as exc:
        logger.exception("GitHub Actions runner error")
        await pub(f"❌ Error: {exc}")
        exit_code = 1

    await pub("__DONE__")
    await r.aclose()

    return exit_code, github_run_url, committed_branch
