"""
Execution Engine
================
Delegates test execution to GitHub Actions via github_actions_runner.py.
The local subprocess (npx playwright test) is kept as a reference but
is no longer invoked directly — GitHub Actions is always used.

Parameter flow:
  run_test() → github_actions_runner.run_test_via_github_actions()
             → stages file to ai-tests-staging branch
             → triggers workflow_dispatch on existing Playwright workflow
             → polls completion, streams updates to Redis → WebSocket
             → if PASS: commits to ai-generated-tests branch
"""
import asyncio
import logging
import os
from pathlib import Path

from config import settings
from github_actions_runner import run_test_via_github_actions, RESULTS_BRANCH

logger = logging.getLogger(__name__)

FRAMEWORK_PATH = Path(settings.PLAYWRIGHT_PROJECT_PATH)


async def run_test(
    run_id: str,
    spec_file: str,       # relative path, e.g. tests/generated/RB001_Module.spec.ts
    script_code: str,     # TypeScript source (needed to stage to GitHub)
    environment: str,
    browser: str,
    device: str,
    execution_mode: str,
    browser_version: str,
    tags: list[str],
) -> tuple[int, str, str | None]:
    """
    Executes the Playwright test via GitHub Actions.
    Returns (exit_code, github_run_url, committed_branch | None).
    """
    spec_filename = Path(spec_file).name   # "RB001_Module.spec.ts"
    exit_code, github_run_url, committed_branch = await run_test_via_github_actions(
        run_id=run_id,
        script_code=script_code,
        spec_filename=spec_filename,
        browser=browser,
        environment=environment,
        device=device,
    )
    return exit_code, github_run_url, committed_branch


async def save_script_to_framework(
    typescript_code: str,
    test_script_num: str,
    module: str,
) -> str:
    """
    Writes the generated .spec.ts file into the framework repo's generated dir.
    Returns the relative file path.
    """
    target_dir = FRAMEWORK_PATH / settings.GENERATED_TESTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{test_script_num}_{module.replace(' ', '_')}.spec.ts"
    file_path = target_dir / filename
    file_path.write_text(typescript_code, encoding="utf-8")
    logger.info("Script saved: %s", file_path)

    return str(Path(settings.GENERATED_TESTS_DIR) / filename)
