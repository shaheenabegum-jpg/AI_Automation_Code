"""
FastAPI Backend — AI Test Automation Platform
=============================================
Routes:
  POST /api/parse-excel          → upload .xlsx, return parsed test cases
  POST /api/generate-script      → SSE stream of generated TypeScript
  POST /api/run-test             → enqueue execution, return run_id
  GET  /api/runs                 → list all execution runs
  GET  /api/runs/{run_id}        → single run detail
  GET  /api/reports/{run_id}     → serve Allure HTML report
  GET  /api/scripts              → list all generated scripts
  GET  /api/framework/refresh    → invalidate & re-fetch framework context
  WS   /ws/run/{run_id}          → live log stream
"""
import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, File, Form, UploadFile, WebSocket,
    WebSocketDisconnect, Depends, HTTPException
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, join

from config import settings
from database import get_db, init_db, AsyncSessionLocal
from models import TestCase, GeneratedScript, ExecutionRun, UserPrompt, ValidationStatus, ExecutionStatus
from excel_parser import parse_excel, test_case_to_json
from framework_loader import get_framework_context, invalidate_cache
from llm_orchestrator import stream_script, active_provider_info
from script_validator import validate_with_self_correction
from execution_engine import run_test, save_script_to_framework
from websocket_manager import ws_manager, redis_log_subscriber

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="AI Test Automation Platform", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL, "http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ════════════════════════════════════════════════════════════════════════════════
# 1. EXCEL UPLOAD & PARSE
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/api/parse-excel")
async def parse_excel_endpoint(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx files are supported")

    raw_bytes = await file.read()
    try:
        test_cases = parse_excel(raw_bytes)
    except ValueError as e:
        raise HTTPException(422, str(e))

    saved_ids: list[str] = []
    for tc in test_cases:
        db_tc = TestCase(
            test_script_num=tc.test_script_num,
            module=tc.module,
            test_case_name=tc.test_case_name,
            description=tc.description,
            raw_steps=tc.raw_steps,
            expected_results=tc.expected_results,
            parsed_json=test_case_to_json(tc),
            excel_source=file.filename,
        )
        db.add(db_tc)
        await db.flush()
        saved_ids.append(str(db_tc.id))

    return {
        "message": f"Parsed {len(test_cases)} test cases",
        "test_cases": [
            {
                "id": sid,
                "test_script_num": tc.test_script_num,
                "module": tc.module,
                "test_case_name": tc.test_case_name,
                "description": tc.description,
                "steps_count": len(tc.steps),
                "expected_results": tc.expected_results,
            }
            for sid, tc in zip(saved_ids, test_cases)
        ],
    }


@app.get("/api/test-cases")
async def list_test_cases(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TestCase).order_by(desc(TestCase.created_at)))
    tcs = result.scalars().all()
    return [
        {
            "id": str(tc.id),
            "test_script_num": tc.test_script_num,
            "module": tc.module,
            "test_case_name": tc.test_case_name,
            "description": tc.description,
            "excel_source": tc.excel_source,
            "created_at": tc.created_at.isoformat(),
        }
        for tc in tcs
    ]


# ════════════════════════════════════════════════════════════════════════════════
# 2. SCRIPT GENERATION — SSE streaming
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/llm-provider")
async def get_llm_provider():
    """Returns the current LLM provider config and which keys are set."""
    return active_provider_info()


@app.post("/api/generate-script")
async def generate_script_endpoint(
    test_case_id: str = Form(...),
    user_instruction: str = Form(default=""),
    llm_provider: str = Form(default=""),   # "anthropic" | "gemini" | "" (use .env default)
    db: AsyncSession = Depends(get_db),
):
    tc = await db.get(TestCase, uuid.UUID(test_case_id))
    if not tc:
        raise HTTPException(404, "Test case not found")

    ctx, ctx_hash = get_framework_context()

    # Resolve provider: form param → env default
    provider = llm_provider.strip().lower() or None  # None → orchestrator uses LLM_PROVIDER

    # Pre-create the script record so we have an ID to return immediately.
    # This uses the request-scoped session which is committed when the handler returns.
    script_record = GeneratedScript(
        test_case_id=tc.id,
        typescript_code="",
        framework_version=ctx_hash[:8],
        validation_status=ValidationStatus.pending,
    )
    db.add(script_record)
    await db.flush()
    script_id        = str(script_record.id)
    script_record_id = script_record.id

    # Snapshot all data needed by the generator — the request-scoped `db` session
    # is committed and CLOSED by get_db() when this handler returns (before the
    # generator body runs inside StreamingResponse).  The generator must NOT use
    # the request `db` after that point; it opens its own AsyncSessionLocal instead.
    tc_parsed_json   = tc.parsed_json
    tc_script_num    = tc.test_script_num
    tc_module        = tc.module

    async def event_stream():
        # Send script_id first so the frontend can poll status
        yield f"data: {json.dumps({'type': 'script_id', 'script_id': script_id})}\n\n"

        full_script = ""
        try:
            # Stream chunks from the chosen LLM provider
            async for chunk in stream_script(
                tc_parsed_json, user_instruction, ctx, provider
            ):
                full_script += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"

            # Safety net 1 — correct ../ → ../../ for scripts in tests/generated/
            full_script = _fix_import_paths(full_script)
            # Safety net 2 — convert named imports to default imports for page/custom classes
            full_script = _fix_page_import_style(full_script)
            # Safety net 3 — auto-add imports for any page class used but not imported
            full_script = _ensure_imports_match_usage(full_script)

            # Validate
            yield f"data: {json.dumps({'type': 'status', 'message': 'Validating TypeScript…'})}\n\n"
            is_valid, errors = await _validate(full_script)

            # Save .spec.ts file to the local framework repo
            file_rel_path = await save_script_to_framework(
                full_script, tc_script_num, tc_module
            )

            # ── Persist to DB using a fresh session ──────────────────────────────
            # The request-scoped `db` session was already committed & closed by
            # get_db() when generate_script_endpoint returned StreamingResponse.
            # We MUST open a new session here to avoid operating on a closed session.
            usage = getattr(stream_script, "last_usage", {})
            async with AsyncSessionLocal() as save_db:
                script_rec = await save_db.get(GeneratedScript, script_record_id)
                if script_rec:
                    script_rec.typescript_code    = full_script
                    script_rec.file_path          = file_rel_path
                    script_rec.validation_status  = (
                        ValidationStatus.valid if is_valid else ValidationStatus.invalid
                    )
                    script_rec.validation_errors  = errors if not is_valid else None

                model_used = usage.get("model", provider or "unknown")
                prompt_record = UserPrompt(
                    script_id=script_record_id,
                    prompt_text=user_instruction or "(default)",
                    framework_context_hash=ctx_hash,
                    model_used=model_used,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                )
                save_db.add(prompt_record)
                await save_db.commit()   # ← explicit commit inside dedicated session
            # ─────────────────────────────────────────────────────────────────────

            yield f"data: {json.dumps({'type': 'done', 'script_id': script_id, 'valid': is_valid, 'errors': errors, 'file_path': file_rel_path, 'provider': usage.get('provider', provider)})}\n\n"

        except Exception as e:
            logger.exception("Script generation failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


_IMPORT_PATH_RE = re.compile(
    r"""(from\s+['"])\.\./(fixtures|pages|custom|utils)/""",
    re.MULTILINE,
)

# Matches named imports for page/custom files that use default exports
# e.g.  import { PetsPage } from '../../pages/PetsPage'
#    →  import PetsPage from '../../pages/PetsPage'
_NAMED_PAGE_IMPORT_RE = re.compile(
    r"""import\s*\{\s*(\w+)\s*\}\s+from\s+(['"])(\.\.\/\.\.\/(pages|custom)\/[^'"]+)\2""",
    re.MULTILINE,
)

# All page/custom classes that use `export default` and need explicit imports.
# Key = class name used in code, Value = the canonical import line to inject.
_AUTO_IMPORT_MAP: dict[str, str] = {
    "MainPage":               "import MainPage from '../../pages/MainPage';",
    "PetsPage":               "import PetsPage from '../../pages/PetsPage';",
    "BasePage":               "import BasePage from '../../pages/BasePage';",
    "SkyeAttributeCommands":  "import SkyeAttributeCommands from '../../custom/SkyeAttributeCommands';",
    "BanorteCommands":        "import BanorteCommands from '../../custom/BanorteCommands';",
}


def _fix_import_paths(code: str) -> str:
    """
    Safety net 1: correct one-level-up paths to two-level-up.
      ../fixtures/Fixtures  →  ../../fixtures/Fixtures
    Already-correct ../../ paths are not touched.
    """
    fixed = _IMPORT_PATH_RE.sub(r"\1../../\2/", code)
    if fixed != code:
        logger.info("_fix_import_paths: corrected ../ → ../../")
    return fixed


def _fix_page_import_style(code: str) -> str:
    """
    Safety net 2: page and custom classes use `export default`, so they must be
    imported WITHOUT braces.  Convert named → default imports for pages/ and custom/.
      import { PetsPage } from '../../pages/PetsPage'
      →  import PetsPage from '../../pages/PetsPage'
    Fixture imports (fixtures/) are NOT touched — those ARE named exports.
    """
    fixed = _NAMED_PAGE_IMPORT_RE.sub(r"import \1 from \2\3\2", code)
    if fixed != code:
        logger.info("_fix_page_import_style: converted named → default imports for page classes")
    return fixed


def _ensure_imports_match_usage(code: str) -> str:
    """
    Safety net 3: auto-add missing default imports for any page/custom class
    that is *used* in the script body but not present in the import block.

    Fixes the common LLM mistake of writing `new MainPage(page)` without
    the corresponding `import MainPage from '../../pages/MainPage';`.

    Only adds imports — never removes them (unused imports are harmless in TS
    without noUnusedLocals, and removal risks breaking intentional imports).
    """
    # Partition the file: import lines at top vs the rest (test body)
    lines = code.splitlines(keepends=True)
    import_end_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            import_end_idx = i + 1          # update each time we see an import
        elif stripped == "":
            pass                             # blank lines between imports are OK
        elif import_end_idx > 0:
            break                            # first non-import, non-blank line → body starts

    import_block = "".join(lines[:import_end_idx])
    body          = "".join(lines[import_end_idx:])

    injected: list[str] = []
    for class_name, import_stmt in _AUTO_IMPORT_MAP.items():
        # Is the class name referenced anywhere in the test body?
        if not re.search(r'\b' + class_name + r'\b', body):
            continue
        # Is it already imported as a default import (no braces)?
        if re.search(r'import\s+' + class_name + r'[\s,]', import_block):
            continue
        # Also guard against named import (e.g. import { MainPage } — safety net 2 should
        # have already converted it, but be defensive)
        if re.search(r'import\s*\{[^}]*\b' + class_name + r'\b', import_block):
            continue
        injected.append(import_stmt)
        logger.info("_ensure_imports_match_usage: auto-added import for %s", class_name)

    if not injected:
        return code

    # Append the missing imports right after the existing import block
    return import_block + "\n".join(injected) + "\n" + body


async def _validate(code: str) -> tuple[bool, str]:
    from script_validator import validate_typescript
    return await validate_typescript(code)


# ════════════════════════════════════════════════════════════════════════════════
# 3. SCRIPTS CRUD
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/scripts")
async def list_scripts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(GeneratedScript, TestCase.test_script_num, TestCase.test_case_name)
        .join(TestCase, GeneratedScript.test_case_id == TestCase.id)
        .order_by(desc(GeneratedScript.created_at))
    )
    rows = result.all()
    return [
        {
            "id": str(s.id),
            "test_case_id": str(s.test_case_id),
            "test_script_num": tsn,
            "test_case_name": tcn,
            "file_path": s.file_path,
            "validation_status": s.validation_status,
            "github_branch": s.github_branch,
            "github_commit": s.github_commit,
            "created_at": s.created_at.isoformat(),
        }
        for s, tsn, tcn in rows
    ]


@app.get("/api/scripts/{script_id}")
async def get_script(script_id: str, db: AsyncSession = Depends(get_db)):
    s = await db.get(GeneratedScript, uuid.UUID(script_id))
    if not s:
        raise HTTPException(404, "Script not found")
    return {
        "id": str(s.id),
        "test_case_id": str(s.test_case_id),
        "typescript_code": s.typescript_code,
        "file_path": s.file_path,
        "validation_status": s.validation_status,
        "validation_errors": s.validation_errors,
        "created_at": s.created_at.isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════════
# 4. TEST EXECUTION
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/api/run-test")
async def run_test_endpoint(
    script_id: str = Form(...),
    environment: str = Form(...),
    browser: str = Form(...),
    device: str = Form(...),
    execution_mode: str = Form(...),
    browser_version: str = Form(default="stable"),
    tags: str = Form(default=""),           # comma-separated
    db: AsyncSession = Depends(get_db),
):
    script = await db.get(GeneratedScript, uuid.UUID(script_id))
    if not script:
        raise HTTPException(404, "Script not found")
    if not script.file_path:
        raise HTTPException(400, "Script has not been saved to the framework repo yet")
    if not script.typescript_code:
        raise HTTPException(400, "Script code is missing — regenerate the script")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    run = ExecutionRun(
        script_id=script.id,
        environment=environment,
        browser=browser,
        device=device,
        execution_mode=execution_mode,
        browser_version=browser_version,
        tags=tag_list,
        status=ExecutionStatus.queued,
    )
    db.add(run)
    await db.flush()
    run_id = str(run.id)

    # ── Commit BEFORE spawning the background task ────────────────────────────
    # Race condition: asyncio.create_task() schedules the coroutine but it can
    # start running as soon as the event loop gets a chance — which is often
    # BEFORE get_db's teardown calls await session.commit().
    # _execute_and_update opens its own session and does db.get(run_id).
    # If the run record isn't committed yet it returns None → silent early exit
    # → run_test_via_github_actions is never called → no logs, no GHA trigger.
    # Explicit commit here guarantees the record is visible before the task runs.
    await db.commit()

    # Fire-and-forget background task
    asyncio.create_task(
        _execute_and_update(
            run_id, str(script.id), script.file_path, script.typescript_code,
            environment, browser, device, execution_mode, browser_version, tag_list,
        )
    )

    return {"run_id": run_id, "status": "queued"}


async def _execute_and_update(
    run_id: str,
    script_id: str,
    spec_file: str,
    script_code: str,
    environment: str,
    browser: str,
    device: str,
    execution_mode: str,
    browser_version: str,
    tags: list[str],
):
    from database import AsyncSessionLocal
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if not run:
            return
        run.status = ExecutionStatus.running
        run.start_time = datetime.utcnow()
        await db.commit()

    exit_code, github_run_url, committed_branch = await run_test(
        run_id, spec_file, script_code, environment, browser,
        device, execution_mode, browser_version, tags,
    )

    async with AsyncSessionLocal() as db:
        # Update execution run
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if run:
            run.end_time = datetime.utcnow()
            run.exit_code = exit_code
            run.status = ExecutionStatus.passed if exit_code == 0 else ExecutionStatus.failed
            run.allure_report_path = github_run_url  # store GitHub run URL as report link
            await db.commit()

        # If test passed, update the script's github_branch
        if exit_code == 0 and committed_branch:
            script = await db.get(GeneratedScript, uuid.UUID(script_id))
            if script:
                script.github_branch = committed_branch
                await db.commit()


@app.get("/api/runs")
async def list_runs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ExecutionRun).order_by(desc(ExecutionRun.start_time))
    )
    runs = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "script_id": str(r.script_id),
            "environment": r.environment,
            "browser": r.browser,
            "device": r.device,
            "execution_mode": r.execution_mode,
            "tags": r.tags or [],
            "status": r.status,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "exit_code": r.exit_code,
            "allure_report_path": r.allure_report_path,
        }
        for r in runs
    ]


@app.get("/api/runs/{run_id}/logs")
async def get_run_logs(run_id: str):
    """
    Returns buffered log lines for a run from the Redis history list.
    Useful as an HTTP fallback if the WebSocket didn't deliver logs.
    """
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.REDIS_URL)
    try:
        history_key = f"run:{run_id}:log_history"
        items = await r.lrange(history_key, 0, -1)
        lines = [
            (item.decode("utf-8") if isinstance(item, bytes) else item)
            for item in items
        ]
        return {"run_id": run_id, "lines": lines, "count": len(lines)}
    finally:
        await r.aclose()


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(ExecutionRun, uuid.UUID(run_id))
    if not r:
        raise HTTPException(404, "Run not found")
    return {
        "id": str(r.id),
        "status": r.status,
        "environment": r.environment,
        "browser": r.browser,
        "device": r.device,
        "execution_mode": r.execution_mode,
        "tags": r.tags or [],
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "exit_code": r.exit_code,
        "logs": r.logs,
        "allure_report_path": r.allure_report_path,
    }


# ════════════════════════════════════════════════════════════════════════════════
# 5. ALLURE REPORT SERVING
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/{run_id}")
async def get_report(run_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.get(ExecutionRun, uuid.UUID(run_id))
    if not r or not r.allure_report_path:
        raise HTTPException(404, "Report not found")
    index = Path(r.allure_report_path) / "index.html"
    if not index.exists():
        raise HTTPException(404, "Report HTML not generated yet")
    return FileResponse(str(index))


# ════════════════════════════════════════════════════════════════════════════════
# 6. FRAMEWORK CACHE CONTROL
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/api/framework/refresh")
def refresh_framework():
    invalidate_cache()
    ctx, ctx_hash = get_framework_context(force_refresh=True)
    return {"message": "Framework context refreshed", "hash": ctx_hash, "chars": len(ctx)}


# ════════════════════════════════════════════════════════════════════════════════
# 7. WEBSOCKET — live logs
# ════════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/run/{run_id}")
async def websocket_run(run_id: str, ws: WebSocket):
    await ws_manager.connect(run_id, ws)
    subscriber_task = asyncio.create_task(
        redis_log_subscriber(run_id, ws_manager, settings.REDIS_URL)
    )
    try:
        while True:
            await ws.receive_text()   # keep connection alive; client may send pings
    except WebSocketDisconnect:
        pass
    finally:
        subscriber_task.cancel()
        ws_manager.disconnect(run_id, ws)


# ════════════════════════════════════════════════════════════════════════════════
# 8. HEALTH CHECK
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok"}
