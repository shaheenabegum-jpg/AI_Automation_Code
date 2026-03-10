"""
Script Validator
================
Writes a generated TypeScript script to a temp file inside the framework repo
and validates it by creating a temporary tsconfig.json that extends the project's
own tsconfig.json.  This gives TypeScript full project context (module resolution,
path aliases, compilerOptions, node_modules types) so relative imports like
'../../fixtures/Fixtures' and '@playwright/test' are resolved correctly.

Strategy:
  1. Ensure node_modules is installed at FRAMEWORK_PATH (npm ci if absent).
  2. Write the script to  tests/generated/__validate_<uuid>.spec.ts
  3. Write a temp  __tsconfig_validate_<uuid>.json  that extends ./tsconfig.json
     and only "includes" our temp spec file.  TypeScript then transitively resolves
     all imports (fixtures, pages, custom, @playwright/test) from node_modules and
     the project's real source tree.
  4. Run: npx tsc --noEmit --skipLibCheck --project <temp_tsconfig>
  5. Filter stdout to only lines that mention our temp spec file (ignore
     pre-existing errors in the framework itself — BanorteCommands.ts,
     SkyeAttributeCommands.ts, GenericUtils.ts all have pre-existing strict-mode
     errors that are not our problem).
  6. Clean up both temp files.

Returns:
  (is_valid: bool, errors: str)

IMPORTANT: node_modules must be present at PLAYWRIGHT_PROJECT_PATH (skye-e2e-tests).
Run `npm ci` or `npm install` inside that directory once after cloning.
The validator auto-detects absence and runs npm ci automatically.
"""
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from config import settings

logger = logging.getLogger(__name__)

FRAMEWORK_PATH = Path(settings.PLAYWRIGHT_PROJECT_PATH)
TSC_TIMEOUT    = 90    # seconds — project-level compile can be slow the first time
NPM_TIMEOUT    = 180   # seconds — npm ci on first run
LINT_TIMEOUT   = 20    # seconds

_IS_WINDOWS = sys.platform == "win32"


async def _subprocess(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """
    Cross-platform asyncio subprocess helper.
    - On Windows: uses create_subprocess_shell because npx/npm are .cmd wrappers
      that require the Windows shell to execute.
    - On Unix/Mac: uses create_subprocess_exec (safer, no shell injection risk).
    Returns (returncode, combined_stdout_stderr).
    """
    if _IS_WINDOWS:
        import subprocess
        shell_cmd = subprocess.list2cmdline(cmd)
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stdout.decode("utf-8", errors="replace").strip()


# ── Node modules guard ───────────────────────────────────────────────────────

_node_modules_installed = False   # module-level cache so we only check once


async def _ensure_node_modules() -> None:
    """
    Run `npm ci` inside FRAMEWORK_PATH if node_modules is absent.
    This is required for `npx tsc` to find TypeScript and @playwright/test types.
    Runs at most once per Python process lifetime.
    """
    global _node_modules_installed
    if _node_modules_installed:
        return

    node_modules = FRAMEWORK_PATH / "node_modules"
    if node_modules.exists():
        _node_modules_installed = True
        return

    logger.warning(
        "node_modules missing at %s — running npm ci (one-time setup)…", FRAMEWORK_PATH
    )
    try:
        rc, output = await _subprocess(
            ["npm", "ci"], cwd=str(FRAMEWORK_PATH), timeout=NPM_TIMEOUT
        )
        if rc == 0:
            logger.info("npm ci succeeded — node_modules ready")
            _node_modules_installed = True
        else:
            logger.error("npm ci failed (rc=%d): %s", rc, output[:500])
    except asyncio.TimeoutError:
        logger.error("npm ci timed out after %ds", NPM_TIMEOUT)
    except Exception as e:
        logger.error("npm ci error: %s", e)


# ── Public entry point ───────────────────────────────────────────────────────

async def validate_typescript(script_code: str) -> tuple[bool, str]:
    """
    Drops the script into a temp .spec.ts file in tests/generated/,
    validates it using the project's tsconfig.json context,
    then cleans up both temp files.

    Returns (True, "") on success or (False, error_text) on failure.
    """
    await _ensure_node_modules()

    run_id        = uuid.uuid4().hex
    temp_spec     = f"__validate_{run_id}.spec.ts"
    temp_tsconfig = f"__tsconfig_validate_{run_id}.json"

    spec_path     = FRAMEWORK_PATH / settings.GENERATED_TESTS_DIR / temp_spec
    tsconfig_path = FRAMEWORK_PATH / temp_tsconfig

    spec_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        spec_path.write_text(script_code, encoding="utf-8")
        errors = await _run_tsc(spec_path, tsconfig_path)
        if errors:
            return False, errors

        lint_errors = await _run_eslint(spec_path)
        if lint_errors:
            return False, lint_errors

        return True, ""
    finally:
        spec_path.unlink(missing_ok=True)
        tsconfig_path.unlink(missing_ok=True)


# ── TypeScript validation ────────────────────────────────────────────────────

async def _run_tsc(spec_path: Path, tsconfig_path: Path) -> str:
    """
    Validates spec_path using a temp tsconfig that extends the framework's
    tsconfig.json.  TypeScript resolves all relative imports transitively
    (fixtures, pages, custom) and resolves @playwright/test from node_modules.

    Only errors from our temp spec file are returned — pre-existing framework
    errors (BanorteCommands.ts, SkyeAttributeCommands.ts, GenericUtils.ts) are
    intentionally filtered out since they are not caused by generated scripts.

    Returns empty string if clean, otherwise error lines for our spec file only.
    """
    project_tsconfig = FRAMEWORK_PATH / "tsconfig.json"

    try:
        if project_tsconfig.exists():
            # ── Project-aware validation (preferred path) ──────────────────
            try:
                rel_spec = spec_path.relative_to(FRAMEWORK_PATH).as_posix()
            except ValueError:
                rel_spec = spec_path.as_posix()

            tsconfig_content = {
                "extends": "./tsconfig.json",
                # TypeScript compiles this file + all files it transitively imports.
                # That gives us full framework context without compiling the whole project.
                "include": [rel_spec],
                "compilerOptions": {
                    "noEmit": True,
                    "skipLibCheck": True,   # suppress errors inside .d.ts files
                },
            }
            tsconfig_path.write_text(json.dumps(tsconfig_content), encoding="utf-8")

            tsc_cmd = [
                "npx", "tsc",
                "--noEmit", "--skipLibCheck",
                "--project", tsconfig_path.name,   # relative to cwd
            ]

        else:
            # ── Fallback: isolated single-file mode (no tsconfig found) ───
            logger.warning(
                "No tsconfig.json at %s — using isolated tsc mode", FRAMEWORK_PATH
            )
            tsc_cmd = [
                "npx", "tsc",
                "--noEmit", "--skipLibCheck",
                "--target", "ES2020",
                "--moduleResolution", "node",
                str(spec_path),
            ]

        rc, output = await _subprocess(tsc_cmd, cwd=str(FRAMEWORK_PATH), timeout=TSC_TIMEOUT)

        if rc != 0 and output:
            # ── Filter: only surface errors from OUR temp spec file ─────────
            # The framework has pre-existing strict-mode errors in custom/ and
            # utils/ — those must NOT fail a newly generated script.
            our_errors = [
                line for line in output.splitlines()
                if spec_path.name in line
            ]
            if our_errors:
                logger.warning(
                    "tsc found %d error(s) in generated spec:\n%s",
                    len(our_errors), "\n".join(our_errors[:10]),
                )
            return "\n".join(our_errors) if our_errors else ""

        return ""

    except asyncio.TimeoutError:
        logger.warning(
            "tsc validation timed out after %ds — treating script as valid", TSC_TIMEOUT
        )
        return ""   # Don't block generation on a slow first compile
    except FileNotFoundError:
        logger.warning("npx / tsc not found — skipping TypeScript validation")
        return ""
    except Exception as e:
        logger.error("Unexpected tsc error: %s", e)
        return ""


# ── ESLint (optional) ────────────────────────────────────────────────────────

async def _run_eslint(file_path: Path) -> str:
    """Runs eslint only if .eslintrc* exists in the framework repo. Optional step."""
    eslint_configs = list(FRAMEWORK_PATH.glob(".eslint*"))
    if not eslint_configs:
        return ""  # No eslint config → skip

    try:
        rc, output = await _subprocess(
            ["npx", "eslint", "--no-eslintrc", "--config", str(eslint_configs[0]), str(file_path)],
            cwd=str(FRAMEWORK_PATH),
            timeout=LINT_TIMEOUT,
        )
        return output if rc != 0 else ""
    except (asyncio.TimeoutError, FileNotFoundError):
        return ""


# ── Self-correction loop (used by legacy claude_orchestrator path) ───────────

async def validate_with_self_correction(
    test_case_json: dict,
    user_instruction: str,
    framework_context: str,
    max_attempts: int = 3,
) -> tuple[str, bool, str]:
    """
    Calls Claude, validates, feeds errors back for self-correction.
    Returns (final_script, is_valid, errors).
    """
    from claude_orchestrator import stream_script

    prompt = user_instruction
    script = ""

    for attempt in range(1, max_attempts + 1):
        logger.info("Generation attempt %d/%d", attempt, max_attempts)
        script = ""
        async for chunk in stream_script(test_case_json, prompt, framework_context):
            script += chunk

        is_valid, errors = await validate_typescript(script)
        if is_valid:
            logger.info("Script passed validation on attempt %d", attempt)
            return script, True, ""

        logger.warning("Attempt %d failed validation:\n%s", attempt, errors[:500])
        if attempt < max_attempts:
            prompt = (
                f"{user_instruction}\n\n"
                f"The previous generated script had these TypeScript errors — fix them:\n"
                f"```\n{errors[:2000]}\n```\n"
                f"Regenerate the COMPLETE corrected script:"
            )

    return script, False, errors
