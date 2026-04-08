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
    WebSocketDisconnect, Depends, HTTPException, Query, Body
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, join

from config import settings
from database import get_db, init_db, AsyncSessionLocal
from models import (
    Project, TestCase, GeneratedScript, ExecutionRun, UserPrompt,
    ValidationStatus, ExecutionStatus, DomSnapshot,
)
from excel_parser import parse_excel, test_case_to_json
from framework_loader import get_framework_context, invalidate_cache
from llm_orchestrator import stream_script, active_provider_info, stream_fix_script
from script_validator import validate_with_self_correction
from execution_engine import run_test, save_script_to_framework, run_test_locally
from github_actions_runner import (
    list_spec_files_from_branch,
    ensure_ai_tests_branch,
    commit_spec_to_ai_branch,
    run_existing_spec_via_gha,
    run_mga_via_gha,
    AI_TESTS_BRANCH,
)
from websocket_manager import ws_manager, redis_log_subscriber, redis_json_subscriber

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
    project_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx files are supported")

    raw_bytes = await file.read()
    try:
        test_cases = parse_excel(raw_bytes)
    except ValueError as e:
        raise HTTPException(422, str(e))

    pid = uuid.UUID(project_id) if project_id.strip() else None

    saved_ids: list[str] = []
    for tc in test_cases:
        db_tc = TestCase(
            project_id=pid,
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
async def list_test_cases(project_id: str = "", db: AsyncSession = Depends(get_db)):
    q = select(TestCase)
    if project_id.strip():
        q = q.where(TestCase.project_id == uuid.UUID(project_id))
    result = await db.execute(q.order_by(desc(TestCase.created_at)))
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


@app.post("/api/crawl-page")
async def crawl_page_endpoint(
    url: str = Form(...),
    project_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Crawl a URL (with optional auto-login from project creds), save snapshot to DB."""
    import hashlib as _hl
    from dom_crawler import crawl_page
    from dom_chunker import build_dom_context

    # Load project credentials for auto-login
    auth = None
    if project_id.strip():
        try:
            proj = await db.get(Project, uuid.UUID(project_id))
            if proj and (proj.pw_email or proj.pw_testuser):
                auth = {
                    "pw_host": proj.pw_host or "",
                    "pw_email": proj.pw_email or "",
                    "pw_password": proj.pw_password or "",
                    "pw_testuser": proj.pw_testuser or "",
                }
                logger.info("Crawl with auth for project %s", proj.name)
        except Exception as e:
            logger.warning("Failed to load project creds for crawl: %s", e)

    result = await crawl_page(url.strip(), auth=auth)
    if result.get("error"):
        raise HTTPException(502, result["error"])

    # Build chunked DOM context
    dom_ctx = build_dom_context(result)

    # Save to PostgreSQL
    snapshot_id = None
    try:
        async with AsyncSessionLocal() as db:
            snapshot = DomSnapshot(
                project_id=uuid.UUID(project_id) if project_id.strip() else None,
                url=result.get("url", url.strip()),
                url_hash=_hl.sha256(url.strip().encode()).hexdigest(),
                title=result.get("title", ""),
                element_count=result.get("element_count", 0),
                elements=result.get("elements", []),
                accessibility_tree=result.get("accessibility_tree", ""),
                screenshot_b64=result.get("screenshot_b64", ""),
                dom_context=dom_ctx,
            )
            db.add(snapshot)
            await db.commit()
            snapshot_id = str(snapshot.id)
            logger.info("Saved DOM snapshot %s for %s", snapshot_id, url.strip())
    except Exception as e:
        logger.warning("Failed to save DOM snapshot: %s", e)

    return {
        "snapshot_id": snapshot_id,
        "url": result.get("url", ""),
        "title": result.get("title", ""),
        "screenshot_b64": result.get("screenshot_b64", ""),
        "element_count": result.get("element_count", 0),
        "elements_preview": result.get("elements", [])[:20],
        "login_status": result.get("login_status"),
    }


@app.get("/api/dom-snapshots")
async def list_dom_snapshots(
    project_id: str = "",
    url: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List DOM snapshots — lightweight (no screenshot/elements in list view)."""
    q = select(DomSnapshot).order_by(desc(DomSnapshot.created_at)).limit(limit)
    if project_id.strip():
        q = q.where(DomSnapshot.project_id == uuid.UUID(project_id))
    if url.strip():
        import hashlib as _hl
        q = q.where(DomSnapshot.url_hash == _hl.sha256(url.strip().encode()).hexdigest())
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": str(s.id),
            "project_id": str(s.project_id) if s.project_id else None,
            "url": s.url,
            "title": s.title,
            "element_count": s.element_count,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in rows
    ]


@app.get("/api/dom-snapshots/{snapshot_id}")
async def get_dom_snapshot(snapshot_id: str, db: AsyncSession = Depends(get_db)):
    """Get full DOM snapshot detail including elements + screenshot."""
    try:
        sid = uuid.UUID(snapshot_id)
    except ValueError:
        raise HTTPException(400, "Invalid snapshot_id")
    s = await db.get(DomSnapshot, sid)
    if not s:
        raise HTTPException(404, "Snapshot not found")
    return {
        "id": str(s.id),
        "project_id": str(s.project_id) if s.project_id else None,
        "url": s.url,
        "title": s.title,
        "element_count": s.element_count,
        "elements": s.elements or [],
        "accessibility_tree": s.accessibility_tree or "",
        "screenshot_b64": s.screenshot_b64 or "",
        "dom_context": s.dom_context or "",
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


@app.get("/api/dom-snapshots/{snapshot_id}/compare/{other_id}")
async def compare_dom_snapshots(
    snapshot_id: str, other_id: str, db: AsyncSession = Depends(get_db),
):
    """Compare two DOM snapshots — shows added/removed elements."""
    try:
        sid_a = uuid.UUID(snapshot_id)
        sid_b = uuid.UUID(other_id)
    except ValueError:
        raise HTTPException(400, "Invalid snapshot ID")

    snap_a = await db.get(DomSnapshot, sid_a)
    snap_b = await db.get(DomSnapshot, sid_b)
    if not snap_a or not snap_b:
        raise HTTPException(404, "One or both snapshots not found")

    # Build selector sets for comparison
    def _selectors(snap):
        return {
            el.get("selector", ""): el
            for el in (snap.elements or [])
            if el.get("selector")
        }

    sels_a = _selectors(snap_a)
    sels_b = _selectors(snap_b)

    added_keys = set(sels_b.keys()) - set(sels_a.keys())
    removed_keys = set(sels_a.keys()) - set(sels_b.keys())
    common_keys = set(sels_a.keys()) & set(sels_b.keys())

    # Detect text changes in common elements
    changed = []
    for k in common_keys:
        if sels_a[k].get("text") != sels_b[k].get("text"):
            changed.append({
                "selector": k,
                "old_text": sels_a[k].get("text", ""),
                "new_text": sels_b[k].get("text", ""),
            })

    return {
        "snapshot_a": {"id": str(snap_a.id), "url": snap_a.url, "title": snap_a.title, "created_at": snap_a.created_at.isoformat()},
        "snapshot_b": {"id": str(snap_b.id), "url": snap_b.url, "title": snap_b.title, "created_at": snap_b.created_at.isoformat()},
        "added_elements": [sels_b[k] for k in added_keys],
        "removed_elements": [sels_a[k] for k in removed_keys],
        "changed_elements": changed,
        "summary": {
            "added": len(added_keys),
            "removed": len(removed_keys),
            "changed": len(changed),
            "unchanged": len(common_keys) - len(changed),
            "total_a": snap_a.element_count,
            "total_b": snap_b.element_count,
        },
    }


@app.post("/api/generate-script")
async def generate_script_endpoint(
    test_case_id: str = Form(...),
    user_instruction: str = Form(default=""),
    llm_provider: str = Form(default=""),   # "anthropic" | "gemini" | "" (use .env default)
    project_id: str = Form(default=""),
    page_url: str = Form(default=""),        # optional: URL to crawl for DOM context
    db: AsyncSession = Depends(get_db),
):
    tc = await db.get(TestCase, uuid.UUID(test_case_id))
    if not tc:
        raise HTTPException(404, "Test case not found")

    # Load project config if project_id provided
    proj_cfg = None
    pid = None
    if project_id.strip():
        pid = uuid.UUID(project_id)
        proj_cfg = await get_project_config(project_id, db)

    ctx, ctx_hash = get_framework_context()

    # DOM context — crawl the page if a URL was provided
    _dom_ctx = ""
    if page_url.strip():
        try:
            from dom_crawler import crawl_page
            from dom_chunker import build_dom_context
            crawl_result = await crawl_page(page_url.strip())
            if not crawl_result.get("error"):
                _dom_ctx = build_dom_context(crawl_result, tc.parsed_json if tc else None)
                logger.info("DOM context: %d chars from %s", len(_dom_ctx), page_url.strip())
        except Exception as e:
            logger.warning("DOM crawl failed (continuing without): %s", e)

    # Resolve provider: form param → env default
    provider = llm_provider.strip().lower() or None  # None → orchestrator uses LLM_PROVIDER

    # Pre-create the script record so we have an ID to return immediately.
    script_record = GeneratedScript(
        test_case_id=tc.id,
        project_id=pid,
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
    dom_ctx          = _dom_ctx

    async def event_stream():
        # Send script_id first so the frontend can poll status
        yield f"data: {json.dumps({'type': 'script_id', 'script_id': script_id})}\n\n"

        full_script = ""
        try:
            # Stream chunks from the chosen LLM provider
            async for chunk in stream_script(
                tc_parsed_json, user_instruction, ctx, provider, dom_context=dom_ctx
            ):
                full_script += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"

            # Safety net 0 — strip markdown fences & multi-file headers
            full_script = _strip_markdown_fences(full_script)
            # Safety net 1 — correct ../ → ../../ for scripts in tests/generated/
            full_script = _fix_import_paths(full_script)
            # Safety net 2 — convert named imports to default imports for page/custom classes
            full_script = _fix_page_import_style(full_script)
            # Safety net 3 — auto-add imports for any page class used but not imported
            full_script = _ensure_imports_match_usage(full_script)

            # POM extraction — detect page class markers and save separately
            page_class_path = None
            pom_dir = proj_cfg["playwright_project_path"] if proj_cfg and proj_cfg.get("playwright_project_path") else settings.PLAYWRIGHT_PROJECT_PATH
            full_script, page_class_path = _extract_and_save_page_class(full_script, pom_dir)
            if page_class_path:
                yield f"data: {json.dumps({'type': 'status', 'message': f'Extracted page class → {page_class_path}'})}\n\n"
                try:
                    page_content = (Path(pom_dir) / page_class_path).read_text(encoding="utf-8")
                    await commit_spec_to_ai_branch(Path(page_class_path).name, page_content)
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Committed {page_class_path} to GitHub'})}\n\n"
                except Exception as pom_err:
                    logger.warning("Failed to commit page class: %s", pom_err)

            # Validate
            yield f"data: {json.dumps({'type': 'status', 'message': 'Validating TypeScript…'})}\n\n"
            is_valid, errors = await _validate(full_script)

            # Save .spec.ts file to the local framework repo
            file_rel_path = await save_script_to_framework(
                full_script, tc_script_num, tc_module
            )

            # Also commit to the AI tests branch on GitHub
            spec_filename = Path(file_rel_path).name
            try:
                commit_sha = await commit_spec_to_ai_branch(spec_filename, full_script)
                logger.info("Spec committed to '%s' branch: %s", AI_TESTS_BRANCH, commit_sha[:8])
                yield f"data: {json.dumps({'type': 'status', 'message': f'Committed to {AI_TESTS_BRANCH} branch'})}\n\n"
            except Exception as commit_err:
                logger.warning("Failed to commit spec to %s: %s", AI_TESTS_BRANCH, commit_err)

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
# MGA framework pages: MainPage, LoginPage, BasePage
_AUTO_IMPORT_MAP: dict[str, str] = {
    "MainPage":               "import MainPage from '../../pages/MainPage';",
    "LoginPage":              "import LoginPage from '../../pages/LoginPage';",
    "BasePage":               "import BasePage from '../../pages/BasePage';",
    "SkyeAttributeCommands":  "import SkyeAttributeCommands from '../../custom/SkyeAttributeCommands';",
    "MGACommands":            "import MGACommands from '../../custom/MGACommands';",
}


def _strip_markdown_fences(code: str) -> str:
    """
    Safety net 0: strip markdown code fences and multi-file headers.
    LLM sometimes wraps output in ```typescript ... ``` or adds **File 1:** headers.
    This extracts the last (or largest) TypeScript code block, which is the .spec.ts.
    """
    import re
    # If the code has markdown fences, extract the code blocks
    blocks = re.findall(r'```(?:typescript|ts)?\s*\n(.*?)```', code, re.DOTALL)
    if blocks:
        # Pick the last block — usually the .spec.ts (page classes come first)
        # But prefer the block that contains 'test(' if there are multiple
        spec_block = None
        for b in blocks:
            if "test(" in b or "test.describe(" in b:
                spec_block = b
        code = spec_block.strip() if spec_block else blocks[-1].strip()
    else:
        # No fences — strip any preamble text before the first import
        lines = code.split('\n')
        start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith('import '):
                start = i
                break
        if start > 0:
            code = '\n'.join(lines[start:])
    return code.strip()


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


def _extract_error_from_logs(log_lines: list[str]) -> str:
    """Extract meaningful error text from Playwright test output logs."""
    import re as _re
    error_patterns = [
        r'Error:', r'error:', r'expect\(', r'toBeVisible', r'toHaveText',
        r'timeout', r'Timeout', r'assert', r'FAILED', r'waiting for',
        r'locator\.', r'page\.', r'at /', r'at C:', r'› ',
        r'browserType\.launch', r'net::ERR',
    ]
    combined_pattern = '|'.join(error_patterns)
    filtered = []
    for line in log_lines:
        if line in ('__DONE__', '') or line.startswith('─' * 5):
            continue
        clean = _re.sub(r'\x1b\[[0-9;]*m', '', line)  # strip ANSI
        if _re.search(combined_pattern, clean):
            filtered.append(clean.strip())
    result = '\n'.join(filtered)
    return result[:2000] if len(result) > 2000 else result


def _extract_and_save_page_class(
    full_script: str, project_dir: str
) -> tuple[str, str | None]:
    """
    Detect POM marker in LLM output. If found, extract page class and save it.
    Returns (spec_code, page_class_path) or (full_script, None) if no marker.
    """
    import re as _re
    marker_match = _re.search(
        r'//\s*===\s*PAGE_CLASS:\s*(\w+)\.ts\s*===', full_script
    )
    if not marker_match:
        return full_script, None

    class_name = marker_match.group(1)
    logger.info("POM detected: %s.ts", class_name)

    # Split on SPEC_FILE marker
    spec_marker = _re.search(r'//\s*===\s*SPEC_FILE\s*===', full_script)
    if not spec_marker:
        logger.warning("PAGE_CLASS marker found but no SPEC_FILE marker — returning as single file")
        return full_script, None

    page_class_code = full_script[marker_match.end():spec_marker.start()].strip()
    spec_code = full_script[spec_marker.end():].strip()

    if not page_class_code or not spec_code:
        logger.warning("POM extraction yielded empty code — returning as single file")
        return full_script, None

    # Save page class file
    pages_dir = Path(project_dir) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    page_file = pages_dir / f"{class_name}.ts"
    page_file.write_text(page_class_code, encoding="utf-8")
    logger.info("Saved page class: %s", page_file)

    return spec_code, f"pages/{class_name}.ts"


# ════════════════════════════════════════════════════════════════════════════════
# 2b. FIX FAILED SCRIPT (Self-Healing)
# ════════════════════════════════════════════════════════════════════════════════

@app.post("/api/fix-script")
async def fix_script_endpoint(
    run_id: str = Form(...),
    llm_provider: str = Form(default=""),
    project_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """SSE stream: auto-fix a failed test script using LLM error analysis."""
    import redis.asyncio as aioredis
    import traceback as _tb

    try:
        return await _fix_script_inner(run_id, llm_provider, project_id, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fix-script error: %s\n%s", repr(e), _tb.format_exc())
        raise HTTPException(500, f"Fix endpoint error: {repr(e)}")


async def _fix_script_inner(run_id, llm_provider, project_id, db):
    import redis.asyncio as aioredis

    # 1. Fetch the failed run
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(400, "Invalid run_id format")
    run = await db.get(ExecutionRun, run_uuid)
    if not run:
        raise HTTPException(404, "Run not found")
    run_status_str = run.status.value if hasattr(run.status, 'value') else str(run.status)
    if run_status_str not in ("failed", "error"):
        raise HTTPException(400, f"Run status is '{run_status_str}', not 'failed'")

    # 2. Get original script code
    original_code = ""
    if run.script_id:
        script = await db.get(GeneratedScript, run.script_id)
        if script:
            original_code = script.typescript_code or ""
    if not original_code and run.spec_file_path:
        # Try reading from disk
        for base in [settings.PLAYWRIGHT_PROJECT_PATH, settings.MGA_PLAYWRIGHT_PROJECT_PATH]:
            if not base:
                continue
            spec_path = Path(base) / run.spec_file_path
            alt_path = Path(base) / run.spec_file_path.removeprefix("skye-e2e-tests/")
            for p in [spec_path, alt_path]:
                if p.exists():
                    original_code = p.read_text(encoding="utf-8")
                    break
            if original_code:
                break
    if not original_code:
        raise HTTPException(400, "Could not find original script code for this run")

    # 3. Fetch error logs from Redis
    r = aioredis.from_url(settings.REDIS_URL)
    history_key = f"run:{run_id}:log_history"
    try:
        raw_lines = await r.lrange(history_key, 0, -1)
        log_lines = [line.decode() if isinstance(line, bytes) else line for line in raw_lines]
    except Exception:
        log_lines = []
    finally:
        await r.aclose()

    error_text = _extract_error_from_logs(log_lines)
    if not error_text:
        error_text = f"Test failed with exit code {run.exit_code or 1}. No detailed error captured."

    # 4. Framework context
    ctx, ctx_hash = get_framework_context()
    provider = llm_provider.strip().lower() or None

    # 5. Pre-create new script record
    pid = uuid.UUID(project_id) if project_id.strip() else run.project_id
    tc_id = None
    if run.script_id:
        orig_script = await db.get(GeneratedScript, run.script_id)
        if orig_script:
            tc_id = orig_script.test_case_id
    if not tc_id:
        # Fallback: find any test case in the project (or the first one)
        from sqlalchemy import select as _sel
        tc_query = _sel(TestCase.id)
        if pid:
            tc_query = tc_query.where(TestCase.project_id == pid)
        tc_query = tc_query.limit(1)
        tc_row = (await db.execute(tc_query)).scalar_one_or_none()
        tc_id = tc_row or None
    if not tc_id:
        raise HTTPException(400, "No test cases found to associate with the fix. Upload an Excel file first.")
    fix_record = GeneratedScript(
        test_case_id=tc_id,
        project_id=pid,
        typescript_code="",
        framework_version=ctx_hash[:8],
        validation_status=ValidationStatus.pending,
    )
    db.add(fix_record)
    await db.flush()
    fix_id = str(fix_record.id)
    fix_record_id = fix_record.id
    await db.commit()

    # Snapshot for closure
    _original_code = original_code
    _error_text = error_text
    _ctx = ctx
    _ctx_hash = ctx_hash

    async def event_stream():
        yield f"data: {json.dumps({'type': 'script_id', 'script_id': fix_id})}\n\n"
        yield f"data: {json.dumps({'type': 'status', 'message': 'Analyzing error and generating fix…'})}\n\n"

        full_script = ""
        try:
            async for chunk in stream_fix_script(
                _original_code, _error_text, _ctx, provider
            ):
                full_script += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'text': chunk})}\n\n"

            # Safety nets
            full_script = _strip_markdown_fences(full_script)
            full_script = _fix_import_paths(full_script)
            full_script = _fix_page_import_style(full_script)
            full_script = _ensure_imports_match_usage(full_script)

            # Validate
            yield f"data: {json.dumps({'type': 'status', 'message': 'Validating fixed script…'})}\n\n"
            is_valid, errors = await _validate(full_script)

            # Save to framework + GitHub
            file_rel_path = ""
            try:
                from execution_engine import save_script_to_framework
                file_rel_path = await save_script_to_framework(
                    full_script, f"FIX_{run_id[:8]}", "auto_fix"
                )
            except Exception as e:
                logger.warning("Failed to save fix to framework: %s", e)

            try:
                from github_actions_runner import commit_spec_to_ai_branch
                spec_filename = Path(file_rel_path).name if file_rel_path else f"FIX_{run_id[:8]}.spec.ts"
                await commit_spec_to_ai_branch(spec_filename, full_script)
            except Exception as e:
                logger.warning("Failed to commit fix to GitHub: %s", e)

            # Persist to DB
            usage = getattr(stream_fix_script, "last_usage", {})
            async with AsyncSessionLocal() as save_db:
                rec = await save_db.get(GeneratedScript, fix_record_id)
                if rec:
                    rec.typescript_code = full_script
                    rec.file_path = file_rel_path
                    rec.validation_status = (
                        ValidationStatus.valid if is_valid else ValidationStatus.invalid
                    )
                    rec.validation_errors = errors if not is_valid else None
                    await save_db.commit()

            model_used = usage.get("model", settings.ANTHROPIC_MODEL)
            yield f"data: {json.dumps({'type': 'done', 'script_id': fix_id, 'valid': is_valid, 'errors': errors, 'file_path': file_rel_path, 'provider': usage.get('provider', 'anthropic')})}\n\n"

        except Exception as e:
            logger.error("Fix script error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ════════════════════════════════════════════════════════════════════════════════
# 3. SCRIPTS CRUD
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/scripts")
async def list_scripts(project_id: str = "", db: AsyncSession = Depends(get_db)):
    q = (
        select(GeneratedScript, TestCase.test_script_num, TestCase.test_case_name)
        .join(TestCase, GeneratedScript.test_case_id == TestCase.id)
    )
    if project_id.strip():
        q = q.where(GeneratedScript.project_id == uuid.UUID(project_id))
    result = await db.execute(q.order_by(desc(GeneratedScript.created_at)))
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


@app.delete("/api/scripts")
async def delete_all_scripts(db: AsyncSession = Depends(get_db)):
    """Delete ALL data — runs, prompts, scripts, test cases — in FK-safe order."""
    from sqlalchemy import delete as sa_delete
    r1 = await db.execute(sa_delete(ExecutionRun))
    r2 = await db.execute(sa_delete(UserPrompt))
    r3 = await db.execute(sa_delete(GeneratedScript))
    r4 = await db.execute(sa_delete(TestCase))
    await db.commit()
    return {
        "deleted_runs": r1.rowcount,
        "deleted_prompts": r2.rowcount,
        "deleted_scripts": r3.rowcount,
        "deleted_test_cases": r4.rowcount,
    }


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
async def list_runs(project_id: str = "", db: AsyncSession = Depends(get_db)):
    q = select(ExecutionRun)
    if project_id.strip():
        q = q.where(ExecutionRun.project_id == uuid.UUID(project_id))
    result = await db.execute(q.order_by(desc(ExecutionRun.start_time)))
    runs = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id) if r.project_id else None,
            "script_id": str(r.script_id) if r.script_id else None,
            "spec_file_path": r.spec_file_path,
            "spec_branch": r.spec_branch,
            "environment": r.environment,
            "browser": r.browser,
            "device": r.device,
            "execution_mode": r.execution_mode,
            "run_target": r.run_target,
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
        "run_target": r.run_target,
        "tags": r.tags or [],
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "exit_code": r.exit_code,
        "logs": r.logs,
        "allure_report_path": r.allure_report_path,
    }


@app.delete("/api/runs")
async def delete_all_runs(db: AsyncSession = Depends(get_db)):
    """Delete ALL run records from the database."""
    from sqlalchemy import delete as sa_delete
    result = await db.execute(sa_delete(ExecutionRun))
    await db.commit()
    return {"deleted": result.rowcount}


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a run record from the database."""
    r = await db.get(ExecutionRun, uuid.UUID(run_id))
    if not r:
        raise HTTPException(404, "Run not found")
    await db.delete(r)
    await db.commit()
    return {"deleted": run_id}


@app.patch("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Mark a queued/running run as cancelled (failed) and set end_time."""
    from datetime import datetime
    r = await db.get(ExecutionRun, uuid.UUID(run_id))
    if not r:
        raise HTTPException(404, "Run not found")
    if r.status not in (ExecutionStatus.queued, ExecutionStatus.running):
        raise HTTPException(400, f"Run is already {r.status} — cannot cancel")
    r.status = ExecutionStatus.failed
    r.end_time = datetime.utcnow()
    r.exit_code = -1
    await db.commit()
    # Publish a cancellation message so the WebSocket client sees it
    import redis.asyncio as aioredis
    red = aioredis.from_url(settings.REDIS_URL)
    try:
        channel = f"run:{run_id}:logs"
        history_key = f"run:{run_id}:log_history"
        await red.publish(channel, "🛑 Run cancelled by user")
        await red.rpush(history_key, "🛑 Run cancelled by user")
        await red.publish(channel, "__DONE__")
        await red.rpush(history_key, "__DONE__")
    finally:
        await red.aclose()
    return {"cancelled": run_id, "status": "failed"}


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
# 8. SPEC FILES — list & run from GitHub branch
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/api/spec-files")
async def list_spec_files(
    branch: str = "",
    project_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    """
    List all .spec.ts files.
    When project_id is provided, fetches from that project's repo/branch.
    Otherwise uses global config.
    """
    proj_cfg = None
    if project_id.strip():
        proj_cfg = await get_project_config(project_id, db)

    # Determine which repo/branch/token to use
    repo = proj_cfg["github_repo"] if proj_cfg else None
    token = proj_cfg["github_token"] if proj_cfg else None
    target_branch = branch.strip() or (proj_cfg["ai_tests_branch"] if proj_cfg else AI_TESTS_BRANCH)

    specs = await list_spec_files_from_branch(target_branch, repo=repo, token=token)

    # Also include specs from staging and results branches for completeness
    if not branch:
        for extra_branch in ["ai-tests-staging", "ai-generated-tests"]:
            extra_specs = await list_spec_files_from_branch(extra_branch, repo=repo, token=token)
            existing_paths = {s["path"] for s in specs}
            for s in extra_specs:
                if s["path"] not in existing_paths:
                    specs.append(s)
                    existing_paths.add(s["path"])

    # ── Local spec files from project's playwright path ──────────────────────
    local_path_str = proj_cfg["playwright_project_path"] if proj_cfg else settings.MGA_PLAYWRIGHT_PROJECT_PATH
    if local_path_str:
        tests_dir = Path(local_path_str) / "tests"
        if tests_dir.exists():
            existing_paths = {s["path"] for s in specs}
            for spec_file in sorted(tests_dir.glob("*.spec.ts")):
                full_path = str(spec_file).replace("\\", "/")
                if full_path not in existing_paths:
                    specs.append({
                        "name": spec_file.name,
                        "path": full_path,
                        "sha": "",
                        "size": spec_file.stat().st_size,
                        "branch": "local-project",
                        "project_id": project_id.strip() or None,
                    })
                    existing_paths.add(full_path)

    return {"specs": specs, "default_branch": target_branch}


@app.post("/api/run-spec")
async def run_spec_endpoint(
    spec_file_path: str = Form(...),    # e.g. "skye-e2e-tests/tests/generated/RB001_Pets.spec.ts"
    branch: str = Form(...),            # branch where the file lives
    environment: str = Form(...),
    browser: str = Form(...),
    device: str = Form(...),
    execution_mode: str = Form(...),
    run_target: str = Form(default="github_actions"),  # "local" or "github_actions"
    tags: str = Form(default=""),
    project_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    """
    Run an existing spec file.
    run_target="local"          → npx playwright test on local machine
    run_target="github_actions" → trigger GitHub Actions workflow
    """
    logger.info("POST /api/run-spec → run_target=%r, execution_mode=%r, browser=%r, env=%r, project_id=%r",
                run_target, execution_mode, browser, environment, project_id)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    pid = uuid.UUID(project_id) if project_id.strip() else None

    # Load project config if provided
    proj_cfg = None
    if pid:
        proj_cfg = await get_project_config(project_id, db)

    run = ExecutionRun(
        project_id=pid,
        script_id=None,
        spec_file_path=spec_file_path,
        spec_branch=branch,
        environment=environment,
        browser=browser,
        device=device,
        execution_mode=execution_mode,
        run_target=run_target,
        browser_version="stable",
        tags=tag_list,
        status=ExecutionStatus.queued,
    )
    db.add(run)
    await db.flush()
    run_id = str(run.id)

    await db.commit()

    if run_target == "local":
        # ── Local execution via subprocess ──────────────────────────────
        project_dir = (
            proj_cfg["playwright_project_path"]
            if proj_cfg and proj_cfg.get("playwright_project_path")
            else settings.MGA_PLAYWRIGHT_PROJECT_PATH
        )
        env_vars = {
            "pw_host": proj_cfg["pw_host"] if proj_cfg else "",
            "pw_testuser": proj_cfg["pw_testuser"] if proj_cfg else "",
            "pw_password": proj_cfg["pw_password"] if proj_cfg else "",
            "pw_email": proj_cfg["pw_email"] if proj_cfg else "",
        }
        # Resolve the correct Playwright --project name for this project.
        # Each project's playwright.config.ts may use different naming:
        #   MGA:     mga-chromium, webkit-auth, chromium-no-auth
        #   Banorte: ai-chromium, ai-firefox, ai-webkit
        # Use ai-chromium for generated tests (matches .*generated\/.*\.spec\.ts)
        # Use mga-chromium for MGA-named tests (matches .*MGA.*\.spec\.ts)
        pw_project_name = None  # None → use default ai-* mapping from execution_engine
        is_mga = False
        if proj_cfg:
            is_mga = "mga" in proj_cfg.get("name", "").lower()
        if not is_mga:
            is_mga = "mga" in project_dir.lower()
        if is_mga and spec_file_path and "generated/" in spec_file_path:
            # Generated specs → ai-chromium project
            pw_project_name = "ai-chromium"
        elif is_mga:
            # Non-generated MGA specs → mga-chromium project
            _mga_browser_map = {
                "chromium": "mga-chromium",
                "firefox":  "mga-chromium",
                "webkit":   "webkit-auth",
            }
            pw_project_name = _mga_browser_map.get(browser.lower())
        asyncio.create_task(
            _execute_local_and_update(
                run_id, spec_file_path, project_dir,
                browser, environment, device, execution_mode, env_vars,
                pw_project_name,
            )
        )
    elif branch == "local-mga":
        # Run the MGA spec via GitHub Actions on AI_Automation_MGA repo
        asyncio.create_task(
            _execute_mga_gha_and_update(
                run_id, spec_file_path, environment, browser, device, execution_mode,
            )
        )
    else:
        asyncio.create_task(
            _execute_spec_and_update(
                run_id, spec_file_path, branch, environment,
                browser, device, execution_mode,
            )
        )

    return {"run_id": run_id, "status": "queued"}


async def _execute_spec_and_update(
    run_id: str,
    spec_file_path: str,
    branch: str,
    environment: str,
    browser: str,
    device: str,
    execution_mode: str,
):
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if not run:
            return
        run.status = ExecutionStatus.running
        run.start_time = datetime.utcnow()
        await db.commit()

    exit_code, github_run_url = await run_existing_spec_via_gha(
        run_id=run_id,
        spec_file_path=spec_file_path,
        branch=branch,
        browser=browser,
        environment=environment,
        device=device,
        execution_mode=execution_mode,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if run:
            run.end_time = datetime.utcnow()
            run.exit_code = exit_code
            run.status = ExecutionStatus.passed if exit_code == 0 else ExecutionStatus.failed
            run.allure_report_path = github_run_url
            await db.commit()


async def _execute_mga_gha_and_update(
    run_id: str,
    spec_file_path: str,   # full local path e.g. C:/.../tests/MGA_Validate.spec.ts
    environment: str,
    browser: str,
    device: str,
    execution_mode: str,
):
    """Run an MGA spec file via GitHub Actions and update the DB run record."""
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if not run:
            return
        run.status = ExecutionStatus.running
        run.start_time = datetime.utcnow()
        await db.commit()

    exit_code, github_run_url = await run_mga_via_gha(
        run_id=run_id,
        spec_file_path=spec_file_path,
        browser=browser,
        environment=environment,
        execution_mode=execution_mode,
        device=device,
    )

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if run:
            run.end_time = datetime.utcnow()
            run.exit_code = exit_code
            run.status = ExecutionStatus.passed if exit_code == 0 else ExecutionStatus.failed
            run.allure_report_path = github_run_url   # reuse field for GHA URL
            await db.commit()


async def _execute_local_and_update(
    run_id: str,
    spec_file_path: str,
    project_dir: str,
    browser: str,
    environment: str,
    device: str,
    execution_mode: str,
    env_vars: dict[str, str],
    playwright_project: str | None = None,
):
    """Run a spec locally via subprocess and update the DB run record."""
    from datetime import datetime

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if not run:
            return
        run.status = ExecutionStatus.running
        run.start_time = datetime.utcnow()
        await db.commit()

    try:
        exit_code, _ = await run_test_locally(
            run_id=run_id,
            spec_file_path=spec_file_path,
            project_dir=project_dir,
            browser=browser,
            environment=environment,
            device=device,
            execution_mode=execution_mode,
            env_vars=env_vars,
            playwright_project=playwright_project,
        )
    except Exception as exc:
        logger.exception("_execute_local_and_update error for run %s", run_id)
        exit_code = 1

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if run:
            run.end_time = datetime.utcnow()
            run.exit_code = exit_code
            run.status = ExecutionStatus.passed if exit_code == 0 else ExecutionStatus.failed
            await db.commit()


@app.post("/api/ensure-branch")
async def ensure_branch_endpoint():
    """Create the AI tests branch if it doesn't exist."""
    branch = await ensure_ai_tests_branch()
    return {"branch": branch, "message": f"Branch '{branch}' is ready"}


# ════════════════════════════════════════════════════════════════════════════════
# 9. PROJECTS — CRUD + helper
# ════════════════════════════════════════════════════════════════════════════════

def _slugify(name: str) -> str:
    """Convert project name to URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _project_to_dict(p: Project) -> dict:
    """Serialize a Project ORM object, masking secrets."""
    return {
        "id": str(p.id),
        "name": p.name,
        "slug": p.slug,
        "description": p.description,
        "icon_color": p.icon_color,
        "github_repo": p.github_repo,
        "github_token": "****" if p.github_token else None,
        "ai_tests_branch": p.ai_tests_branch,
        "workflow_path": p.workflow_path,
        "playwright_project_path": p.playwright_project_path,
        "generated_tests_dir": p.generated_tests_dir,
        "runner_label": p.runner_label,
        "pw_host": p.pw_host,
        "pw_testuser": p.pw_testuser,
        "pw_password": "****" if p.pw_password else None,
        "pw_email": p.pw_email,
        "framework_fetch_paths": p.framework_fetch_paths,
        "system_prompt_override": p.system_prompt_override,
        "jira_url": p.jira_url,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def get_project_config(project_id: str, db: AsyncSession) -> dict:
    """Load project config dict, falling back to global settings for unset values."""
    p = await db.get(Project, uuid.UUID(project_id))
    if not p:
        raise HTTPException(404, "Project not found")
    return {
        "id": str(p.id),
        "name": p.name,
        "github_repo": p.github_repo,
        "github_token": p.github_token or settings.GITHUB_TOKEN,
        "ai_tests_branch": p.ai_tests_branch or settings.AI_TESTS_BRANCH,
        "playwright_project_path": p.playwright_project_path or settings.PLAYWRIGHT_PROJECT_PATH,
        "generated_tests_dir": p.generated_tests_dir or "tests/generated",
        "runner_label": p.runner_label or "self-hosted",
        "pw_host": p.pw_host,
        "pw_testuser": p.pw_testuser,
        "pw_password": p.pw_password,
        "pw_email": p.pw_email,
        "workflow_path": p.workflow_path,
        "framework_fetch_paths": p.framework_fetch_paths,
        "system_prompt_override": p.system_prompt_override,
    }


@app.get("/api/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).where(Project.is_active == True).order_by(Project.name)
    )
    projects = result.scalars().all()
    return [_project_to_dict(p) for p in projects]


@app.post("/api/projects")
async def create_project(
    data: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    name = data.get("name", "").strip()
    github_repo = data.get("github_repo", "").strip()
    if not name:
        raise HTTPException(400, "Project name is required")
    if not github_repo:
        raise HTTPException(400, "GitHub repo (owner/repo) is required")

    slug = _slugify(name)
    # Check uniqueness
    existing = await db.execute(select(Project).where((Project.name == name) | (Project.slug == slug)))
    if existing.scalars().first():
        raise HTTPException(409, f"Project '{name}' already exists")

    p = Project(
        name=name,
        slug=slug,
        description=data.get("description", ""),
        icon_color=data.get("icon_color", "#6366f1"),
        github_repo=github_repo,
        github_token=data.get("github_token") or None,
        ai_tests_branch=data.get("ai_tests_branch", "ai-playwright-tests"),
        workflow_path=data.get("workflow_path") or None,
        playwright_project_path=data.get("playwright_project_path") or None,
        generated_tests_dir=data.get("generated_tests_dir", "tests/generated"),
        runner_label=data.get("runner_label", "self-hosted"),
        pw_host=data.get("pw_host") or None,
        pw_testuser=data.get("pw_testuser") or None,
        pw_password=data.get("pw_password") or None,
        pw_email=data.get("pw_email") or None,
        framework_fetch_paths=data.get("framework_fetch_paths") or None,
        system_prompt_override=data.get("system_prompt_override") or None,
        jira_url=data.get("jira_url") or None,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _project_to_dict(p)


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, uuid.UUID(project_id))
    if not p:
        raise HTTPException(404, "Project not found")
    return _project_to_dict(p)


@app.put("/api/projects/{project_id}")
async def update_project(
    project_id: str,
    data: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Project, uuid.UUID(project_id))
    if not p:
        raise HTTPException(404, "Project not found")

    # Updatable fields (only update if key is present in data)
    updatable = [
        "name", "description", "icon_color", "github_repo", "github_token",
        "ai_tests_branch", "workflow_path", "playwright_project_path",
        "generated_tests_dir", "runner_label", "pw_host", "pw_testuser",
        "pw_password", "pw_email", "framework_fetch_paths",
        "system_prompt_override", "jira_url", "is_active",
    ]
    for field in updatable:
        if field in data:
            setattr(p, field, data[field])

    # Regenerate slug if name changed
    if "name" in data and data["name"]:
        p.slug = _slugify(data["name"])

    await db.commit()
    await db.refresh(p)
    return _project_to_dict(p)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    p = await db.get(Project, uuid.UUID(project_id))
    if not p:
        raise HTTPException(404, "Project not found")
    p.is_active = False
    await db.commit()
    return {"deleted": project_id, "name": p.name}


# ════════════════════════════════════════════════════════════════════════════════
# 11. HEALTH CHECK
# ════════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok"}
