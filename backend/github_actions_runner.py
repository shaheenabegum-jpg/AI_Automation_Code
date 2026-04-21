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
import queue as _stdlib_queue
import threading
import time
from datetime import datetime, timezone

import httpx
import redis.asyncio as aioredis
import requests as _sync_requests

from config import settings

logger = logging.getLogger(__name__)

STAGING_BRANCH      = "ai-tests-staging"
RESULTS_BRANCH      = "ai-generated-tests"
AI_TESTS_BRANCH     = settings.AI_TESTS_BRANCH   # "ai-playwright-tests"
API_BASE            = "https://api.github.com"

MGA_WORKFLOW_PATH = ".github/workflows/mga-tests.yml"

# GitHub Actions workflow YAML committed to main branch of AI_Automation_MGA repo.
# Triggered via workflow_dispatch; receives spec file + credentials as inputs.
MGA_WORKFLOW_YAML = """\
name: MGA Playwright Tests

on:
  workflow_dispatch:
    inputs:
      test_file:
        description: 'Spec file (relative to skye-e2e-tests/)'
        required: true
        default: 'tests/MGA_Validate.spec.ts'
      branch:
        description: 'Git branch that contains the spec file'
        required: false
        default: 'main'
      browser:
        description: 'Browser (chromium/firefox/webkit)'
        required: false
        default: 'chromium'
      environment:
        description: 'Environment (dev/sit/uat)'
        required: false
        default: 'dev'
      execution_mode:
        description: 'Execution mode (headless/headed)'
        required: false
        default: 'headless'
      device:
        description: 'Device'
        required: false
        default: 'Desktop Chrome'
      pw_host:
        description: 'Test HOST URL'
        required: false
        default: 'https://skye1.dev.mga.innoveo-skye.net'
      pw_testuser:
        description: 'Test username'
        required: false
        default: 'usercc'
      pw_password:
        description: 'Test password'
        required: false
        default: 'MGA@1234'
      pw_email:
        description: 'Test email'
        required: false
        default: 'yash.bodhale+MGAUA@tinubu.com'

jobs:
  mga-tests:
    name: MGA Playwright Tests
    # Self-hosted runner: test app is on a private network not reachable from GitHub cloud.
    runs-on: self-hosted
    defaults:
      run:
        working-directory: skye-e2e-tests
    env:
      pw_HOST: ${{ github.event.inputs.pw_host }}
      pw_TESTUSER: ${{ github.event.inputs.pw_testuser }}
      pw_PASSWORD: ${{ github.event.inputs.pw_password }}
      pw_EMAIL: ${{ github.event.inputs.pw_email }}
      CI: 'true'

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.inputs.branch || 'main' }}
          fetch-depth: 0

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install dependencies
        run: npm ci
        shell: bash

      - name: Install Playwright Chromium
        run: npx playwright install chromium
        shell: bash

      - name: Run MGA Playwright tests
        run: |
          TEST_FILE="${{ github.event.inputs.test_file }}"
          MODE="${{ github.event.inputs.execution_mode }}"
          echo "Running: $TEST_FILE on branch ${{ github.event.inputs.branch }}"
          if [ "$MODE" = "headed" ]; then
            npx playwright test "$TEST_FILE" --project=mga-chromium --headed
          else
            npx playwright test "$TEST_FILE" --project=mga-chromium
          fi
        shell: bash

      - name: Upload Playwright report
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: playwright-report-${{ github.run_id }}
          path: skye-e2e-tests/playwright-report/
          retention-days: 30
"""


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
    """POST workflow_dispatch with inputs."""
    logger.info("_trigger_workflow: branch=%s inputs=%s", branch, inputs)
    resp = await client.post(
        f"{API_BASE}/repos/{_repo()}/actions/workflows/{workflow_id}/dispatches",
        headers=_headers(),
        json={"ref": branch, "inputs": inputs},
    )
    if resp.status_code == 422:
        # Log the error body for debugging — do NOT retry without inputs
        # because that silently drops execution_mode, browser, etc.
        body = resp.text
        logger.error("workflow_dispatch 422: %s", body)
        # Still raise so the caller knows it failed
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
    execution_mode: str = "headless",
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

            # 1. Ensure AI tests branch exists
            await pub(f"📂 Preparing branch '{AI_TESTS_BRANCH}'…")
            await _ensure_branch(client, AI_TESTS_BRANCH)

            # 2. Commit test file to AI tests branch
            await pub(f"📝 Committing {spec_filename} to '{AI_TESTS_BRANCH}'…")
            await _commit_file(
                client,
                branch=AI_TESTS_BRANCH,
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
            # Strip skye-e2e-tests/ prefix — workflow working-directory is already skye-e2e-tests/
            test_file_rel = file_repo_path.removeprefix("skye-e2e-tests/")
            triggered_at = time.time()
            inputs = {
                "test_file":      test_file_rel,
                "branch":         AI_TESTS_BRANCH,
                "browser":        browser,
                "environment":    environment,
                "execution_mode": execution_mode,
                "device":         device,
            }
            # Trigger on 'main' — workflow YAML on main checkouts ai-playwright-tests
            TRIGGER_BRANCH = "main"
            await pub(f"🚀 Triggering workflow on '{TRIGGER_BRANCH}' | mode={execution_mode} | device={device}…")
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
                await pub(f"   File remains in '{AI_TESTS_BRANCH}' for debugging")

    except Exception as exc:
        logger.exception("GitHub Actions runner error")
        await pub(f"❌ Error: {exc}")
        exit_code = 1

    await pub("__DONE__")
    await r.aclose()

    return exit_code, github_run_url, committed_branch


# ── List spec files from GitHub branch ────────────────────────────────────────

async def list_spec_files_from_branch(
    branch: str | None = None,
    *,
    repo: str | None = None,
    token: str | None = None,
) -> list[dict]:
    """
    List all .spec.ts files under skye-e2e-tests/tests/ in the given branch.
    Returns a list of dicts with: name, path, sha, size, branch.

    If `repo` / `token` are provided, use them instead of the global defaults.
    This allows per-project spec listing.
    """
    branch = branch or AI_TESTS_BRANCH
    use_repo = repo or _repo()
    use_headers = _headers()
    if token:
        use_headers = {**use_headers, "Authorization": f"Bearer {token}"}
    specs: list[dict] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Ensure the branch exists first
        resp = await client.get(
            f"{API_BASE}/repos/{use_repo}/git/ref/heads/{branch}",
            headers=use_headers,
        )
        if resp.status_code == 404:
            logger.info("Branch '%s' does not exist in %s", branch, use_repo)
            return []

        # Use recursive tree API to get all files under tests/
        resp = await client.get(
            f"{API_BASE}/repos/{use_repo}/git/trees/{branch}",
            headers=use_headers,
            params={"recursive": "1"},
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch tree for branch '%s' in %s: %s", branch, use_repo, resp.status_code)
            return []

        tree = resp.json().get("tree", [])
        for item in tree:
            path = item.get("path", "")
            if (
                item.get("type") == "blob"
                and path.startswith("skye-e2e-tests/tests/")
                and path.endswith(".spec.ts")
            ):
                filename = path.split("/")[-1]
                specs.append({
                    "name": filename,
                    "path": path,
                    "sha": item.get("sha", ""),
                    "size": item.get("size", 0),
                    "branch": branch,
                })

    specs.sort(key=lambda s: s["name"])
    return specs


# ── Ensure branch exists ─────────────────────────────────────────────────────

async def ensure_ai_tests_branch() -> str:
    """Create the AI tests branch if it doesn't exist. Returns the branch name."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_branch(client, AI_TESTS_BRANCH)
    return AI_TESTS_BRANCH


# ── Commit spec file to AI tests branch ──────────────────────────────────────

async def commit_spec_to_ai_branch(
    spec_filename: str,
    script_code: str,
) -> str:
    """Commit a spec file to the AI tests branch. Returns commit SHA."""
    file_repo_path = f"skye-e2e-tests/tests/generated/{spec_filename}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        await _ensure_branch(client, AI_TESTS_BRANCH)
        sha = await _commit_file(
            client,
            branch=AI_TESTS_BRANCH,
            file_path=file_repo_path,
            content=script_code,
            message=f"feat: add AI-generated test {spec_filename}",
        )
    return sha


# ── Run an existing spec file from a branch via GitHub Actions ────────────────

async def run_existing_spec_via_gha(
    run_id: str,
    spec_file_path: str,   # e.g. "skye-e2e-tests/tests/generated/RB001_Pets.spec.ts"
    branch: str,
    browser: str,
    environment: str,
    device: str,
    execution_mode: str,
) -> tuple[int, str]:
    """
    Trigger GitHub Actions for an existing spec file on a branch.
    Unlike run_test_via_github_actions, this does NOT commit the file first —
    it assumes the file already exists on the branch.
    Returns (exit_code, github_run_url).
    """
    r = aioredis.from_url(settings.REDIS_URL)
    channel = f"run:{run_id}:logs"
    history_key = f"run:{run_id}:log_history"

    async def pub(msg: str) -> None:
        await r.publish(channel, msg)
        await r.rpush(history_key, msg)
        await r.expire(history_key, 86400)

    await asyncio.sleep(2)

    spec_filename = spec_file_path.split("/")[-1]
    mode_emoji = "🖥️" if execution_mode == "headed" else "👻"
    await pub(f"▶ Starting GitHub Actions run [{run_id}]")
    await pub(f"  Repo    : {_repo()}")
    await pub(f"  File    : {spec_file_path}")
    await pub(f"  Branch  : {branch}")
    await pub(f"  Env     : {environment.upper()} | {browser} | {device}")
    await pub(f"  Mode    : {mode_emoji} {execution_mode.upper()}")
    await pub("─" * 60)

    github_run_url: str = ""
    exit_code = 1

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Verify the file exists on the branch
            existing_sha = await _get_file_sha(client, branch, spec_file_path)
            if not existing_sha:
                await pub(f"❌ File not found: {spec_file_path} on branch '{branch}'")
                await pub("__DONE__")
                await r.aclose()
                return 1, ""

            await pub(f"✓ File verified on branch '{branch}'")

            # Discover workflow
            await pub("🔍 Discovering Playwright workflow…")
            workflow_id, workflow_name = await _discover_workflow(client)
            await pub(f"✓ Using workflow: '{workflow_name}' (id={workflow_id})")

            # Trigger workflow_dispatch on main (workflow YAML lives on main).
            # Pass the spec's actual branch so the checkout step uses the right ref.
            # Strip skye-e2e-tests/ prefix — workflow runs inside that directory.
            triggered_at = time.time()
            test_file_rel = spec_file_path.removeprefix("skye-e2e-tests/")
            inputs = {
                "test_file":      test_file_rel,
                "branch":         branch,           # ← checkout the branch that has the file
                "browser":        browser,
                "environment":    environment,
                "execution_mode": execution_mode,
                "device":         device,
            }
            TRIGGER_BRANCH = "main"
            logger.info("run_existing_spec_via_gha: execution_mode=%r, device=%r, inputs=%s", execution_mode, device, inputs)
            await pub(f"🚀 Triggering workflow on '{TRIGGER_BRANCH}' | mode={execution_mode} | device={device}…")
            await _trigger_workflow(client, workflow_id, TRIGGER_BRANCH, inputs)
            await pub("✓ Workflow triggered — polling for completion…")

            conclusion, github_run_url = await _wait_for_run(
                client, workflow_id, TRIGGER_BRANCH, triggered_at, pub
            )
            exit_code = 0 if conclusion == "success" else 1

    except Exception as exc:
        logger.exception("GitHub Actions runner error")
        await pub(f"❌ Error: {exc}")
        exit_code = 1

    await pub("__DONE__")
    await r.aclose()

    return exit_code, github_run_url


# ── MGA GitHub Actions runner (thread-based, avoids Windows SelectorEventLoop) ──

def _mga_sync_worker(
    spec_rel: str,
    browser: str,
    environment: str,
    execution_mode: str,
    device: str,
    repo: str,
    headers: dict,
    msg_q: "_stdlib_queue.Queue",
) -> None:
    """
    Run all MGA GitHub API calls synchronously inside a background thread.
    Uses `requests` (sync) so it never touches the asyncio event loop —
    safe on Windows SelectorEventLoop inside asyncio.create_task.

    Posts tuples to msg_q:
      ("log",  message_str)            — a log line to publish
      ("done", exit_code, html_url)    — final result
      ("error", error_str)             — unhandled exception
    """
    def log(msg: str) -> None:
        msg_q.put(("log", msg))

    try:
        # ── Step 1: Check/create the MGA workflow YAML ──────────────────────
        log("🔧 Checking MGA workflow on main branch…")
        resp = _sync_requests.get(
            f"{API_BASE}/repos/{repo}/actions/workflows",
            headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list workflows: {resp.status_code} {resp.text[:200]}")

        workflow_id: int | None = None
        workflow_name: str = ""
        for wf in resp.json().get("workflows", []):
            wf_path = wf.get("path", "")
            if MGA_WORKFLOW_PATH in wf_path or wf_path.endswith("mga-tests.yml"):
                workflow_id = wf["id"]
                workflow_name = wf["name"]
                break

        if workflow_id is None:
            # Workflow YAML not yet in repo — create it
            log(f"📝 Creating GitHub Actions workflow at {MGA_WORKFLOW_PATH}…")

            # Check if file already exists (need SHA for update)
            file_resp = _sync_requests.get(
                f"{API_BASE}/repos/{repo}/contents/{MGA_WORKFLOW_PATH}",
                headers=headers,
                params={"ref": "main"},
                timeout=30,
            )
            existing_sha: str | None = (
                file_resp.json().get("sha") if file_resp.status_code == 200 else None
            )

            encoded = base64.b64encode(MGA_WORKFLOW_YAML.encode("utf-8")).decode("ascii")
            payload: dict = {
                "message": "ci: add MGA Playwright workflow for AI test platform",
                "content": encoded,
                "branch": "main",
            }
            if existing_sha:
                payload["sha"] = existing_sha

            commit_resp = _sync_requests.put(
                f"{API_BASE}/repos/{repo}/contents/{MGA_WORKFLOW_PATH}",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if commit_resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Failed to commit workflow YAML: "
                    f"{commit_resp.status_code} {commit_resp.text[:300]}"
                )

            log("✓ Workflow file committed — waiting for GitHub to index it…")

            # Retry up to 10 × 3 s = 30 s
            for attempt in range(10):
                time.sleep(3)
                resp2 = _sync_requests.get(
                    f"{API_BASE}/repos/{repo}/actions/workflows",
                    headers=headers, timeout=30,
                )
                if resp2.status_code == 200:
                    for wf in resp2.json().get("workflows", []):
                        wf_path = wf.get("path", "")
                        if MGA_WORKFLOW_PATH in wf_path or wf_path.endswith("mga-tests.yml"):
                            workflow_id = wf["id"]
                            workflow_name = wf["name"]
                            log(f"✓ Workflow indexed: '{workflow_name}' (id={workflow_id})")
                            break
                if workflow_id:
                    break

            if workflow_id is None:
                raise RuntimeError(
                    f"GitHub did not index {MGA_WORKFLOW_PATH} within 30 s — "
                    "check the Actions tab on the repository."
                )
        else:
            log(f"✓ Using workflow: '{workflow_name}' (id={workflow_id})")

        # ── Step 2: Trigger workflow_dispatch ────────────────────────────────
        triggered_at = time.time()
        TRIGGER_BRANCH = "main"
        inputs = {
            "test_file":      spec_rel,
            "browser":        browser,
            "environment":    environment,
            "execution_mode": execution_mode,
            "device":         device,
            "pw_host":        "https://skye1.dev.mga.innoveo-skye.net",
            "pw_testuser":    "usercc",
            "pw_password":    "MGA@1234",
            "pw_email":       "yash.bodhale+MGAUA@tinubu.com",
        }
        log(
            f"🚀 Triggering MGA workflow on '{TRIGGER_BRANCH}' | "
            f"mode={execution_mode} | device={device}…"
        )
        trigger_resp = _sync_requests.post(
            f"{API_BASE}/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers=headers,
            json={"ref": TRIGGER_BRANCH, "inputs": inputs},
            timeout=30,
        )
        if trigger_resp.status_code == 422:
            raise RuntimeError(f"workflow_dispatch 422: {trigger_resp.text}")
        trigger_resp.raise_for_status()
        log("✓ Workflow triggered — polling for completion…")

        # ── Step 3: Wait for the run to appear (up to 60 s) ─────────────────
        log("⏳ Waiting for GitHub Actions runner to pick up the job…")
        poll_run_id: int | None = None
        for _ in range(20):
            time.sleep(3)
            runs_resp = _sync_requests.get(
                f"{API_BASE}/repos/{repo}/actions/workflows/{workflow_id}/runs",
                headers=headers,
                params={"branch": TRIGGER_BRANCH, "per_page": 10},
                timeout=30,
            )
            if runs_resp.status_code != 200:
                continue
            runs = runs_resp.json().get("workflow_runs", [])
            new_runs = [
                r2 for r2 in runs
                if _iso_to_ts(r2.get("created_at", "0")) >= (triggered_at - 10)
            ]
            if new_runs:
                poll_run_id = new_runs[0]["id"]
                break

        if not poll_run_id:
            log("⚠ Could not detect GitHub Actions run — check Actions tab manually.")
            msg_q.put(("done", 1, f"https://github.com/{repo}/actions"))
            return

        html_url = f"https://github.com/{repo}/actions/runs/{poll_run_id}"
        log(f"🔗 GitHub Actions run: {html_url}")

        # ── Step 4: Poll until complete (max 900 s) ──────────────────────────
        deadline = time.time() + 900
        last_status = ""
        while time.time() < deadline:
            time.sleep(5)
            run_resp = _sync_requests.get(
                f"{API_BASE}/repos/{repo}/actions/runs/{poll_run_id}",
                headers=headers, timeout=30,
            )
            if run_resp.status_code != 200:
                continue
            data       = run_resp.json()
            status     = data.get("status", "unknown")
            conclusion = data.get("conclusion")
            elapsed    = int(time.time() - triggered_at)

            status_line = f"{status}" + (f" | {conclusion}" if conclusion else "")
            if status_line != last_status:
                log(f"⏳ GHA status: {status_line} | elapsed={elapsed}s")
                last_status = status_line

            if status == "completed":
                if conclusion == "success":
                    log("✅ GitHub Actions PASSED")
                else:
                    log(f"❌ GitHub Actions FAILED (conclusion={conclusion})")
                log(f"🔗 Full logs: {html_url}")
                msg_q.put(("done", 0 if conclusion == "success" else 1, html_url))
                return

        log("⏰ Timed out waiting for GitHub Actions run")
        msg_q.put(("done", 1, html_url))

    except Exception as exc:
        logger.exception("_mga_sync_worker error")
        msg_q.put(("error", f"{type(exc).__name__}: {exc}"))


async def run_mga_via_gha(
    run_id: str,
    spec_file_path: str,   # full local path e.g. C:/.../tests/MGA_Validate.spec.ts
    browser: str,
    environment: str,
    execution_mode: str,
    device: str,
) -> tuple[int, str]:
    """
    Run an MGA spec file via GitHub Actions on the AI_Automation_MGA repo.
    Uses a background thread + stdlib queue to avoid httpx hanging on Windows
    SelectorEventLoop inside asyncio.create_task.
    Returns (exit_code, github_run_url).
    """
    from pathlib import Path as _Path

    r = aioredis.from_url(settings.REDIS_URL)
    channel     = f"run:{run_id}:logs"
    history_key = f"run:{run_id}:log_history"

    async def pub(msg: str) -> None:
        await r.publish(channel, msg)
        await r.rpush(history_key, msg)
        await r.expire(history_key, 86400)

    # Brief delay so WebSocket client can subscribe before we start publishing
    await asyncio.sleep(2)

    # Derive spec path relative to skye-e2e-tests/
    mga_root = _Path(settings.MGA_PLAYWRIGHT_PROJECT_PATH)
    try:
        spec_rel = str(_Path(spec_file_path).relative_to(mga_root)).replace("\\", "/")
    except ValueError:
        spec_rel = f"tests/{_Path(spec_file_path).name}"

    mode_emoji = "🖥️" if execution_mode == "headed" else "👻"
    await pub(f"▶ Starting MGA GitHub Actions run [{run_id}]")
    await pub(f"  Repo    : {_repo()}")
    await pub(f"  File    : {spec_rel}")
    await pub(f"  Env     : {environment.upper()} | {browser} | {device}")
    await pub(f"  Mode    : {mode_emoji} {execution_mode.upper()}")
    await pub("─" * 60)

    # Queue used by the worker thread to send log lines + final result
    msg_q: _stdlib_queue.Queue = _stdlib_queue.Queue()

    # Snapshot config values for the thread (thread-safe reads)
    repo    = _repo()
    headers = _headers()

    t = threading.Thread(
        target=_mga_sync_worker,
        args=(spec_rel, browser, environment, execution_mode, device, repo, headers, msg_q),
        daemon=True,
    )
    t.start()

    # ── Async drain loop ─────────────────────────────────────────────────────
    exit_code      = 1
    github_run_url = ""
    done           = False

    while not done:
        # Drain all messages the worker has posted so far
        try:
            while True:
                item = msg_q.get_nowait()
                if item[0] == "log":
                    await pub(item[1])
                elif item[0] == "done":
                    exit_code, github_run_url = item[1], item[2]
                    done = True
                    break
                elif item[0] == "error":
                    await pub(f"❌ Error: {item[1]}")
                    done = True
                    break
        except _stdlib_queue.Empty:
            pass

        if not done:
            await asyncio.sleep(0.5)   # yield control back to the event loop

    await pub("__DONE__")
    await r.aclose()

    return exit_code, github_run_url
