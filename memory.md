# memory.md — Full Changelog
> Chronological record of every change made to the AI Test Automation Platform.
> Updated automatically during Claude Code sessions.

---

## Session 1 — Initial Platform Build

### What was built from scratch:

#### Backend (`backend/`)
| File | Description |
|------|-------------|
| `main.py` | FastAPI app with all 12 routes (parse-excel, generate-script SSE, run-test, runs, scripts, reports, framework/refresh, WebSocket, health) |
| `config.py` | Pydantic Settings — reads from `.env` |
| `database.py` | SQLAlchemy async engine + `AsyncSessionLocal` + `get_db` dependency |
| `models.py` | 4 ORM models: `TestCase`, `GeneratedScript`, `ExecutionRun`, `UserPrompt` + enums |
| `excel_parser.py` | Parses `.xlsx` with openpyxl — maps columns: Test Script Num, Module, Test Case, Description, Step, Expected Results |
| `framework_loader.py` | Fetches framework files from GitHub API → stores in Redis (24h TTL) → returns concatenated context |
| `claude_orchestrator.py` | Original Claude-only orchestrator (later superseded) |
| `llm_orchestrator.py` | Multi-provider orchestrator (Anthropic + Gemini) — current active file |
| `script_validator.py` | `tsc --noEmit` TypeScript validation + self-correction retry loop |
| `execution_engine.py` | `npx playwright test` subprocess + Allure report generation |
| `websocket_manager.py` | WebSocket connection manager + Redis pub/sub subscriber |
| `requirements.txt` | All Python dependencies |
| `.env.example` | Template for secrets |
| `alembic.ini` + `alembic/` | DB migrations setup |

#### Frontend (`frontend/`)
| File | Description |
|------|-------------|
| `src/App.tsx` | Dark-themed Ant Design layout with 3 tabs |
| `src/api/client.ts` | Axios + fetch SSE + WebSocket helpers |
| `src/types/index.ts` | TypeScript interfaces (TestCase, Script, Run, RunParams) |
| `src/components/AIPhaseTab.tsx` | Upload → Select → Generate workflow with Monaco editor |
| `src/components/RunTab.tsx` | Test execution UI with live logs terminal |
| `src/components/Dashboard.tsx` | Run history, pie chart, Allure report embed |
| `vite.config.ts` | Vite dev server with proxy config |
| `package.json` | React + Ant Design + Monaco + Recharts dependencies |

#### Framework Integration
| File | Description |
|------|-------------|
| `skye-e2e-tests/playwright.config.ts` | Added 5 ai-* projects without auth dependencies |
| `skye-e2e-tests/tests/generated/` | Created directory for AI-generated test files |

---

## Session 2 — Bug Fixes & Port Issues

### Fix 1: Vite Port Conflict (5173 → 5174)
- **Problem:** Port 5173 was occupied by another application
- **Fix:** Updated `vite.config.ts`: `port: 5174, strictPort: true`
- **Fix:** Updated `backend/.env`: `FRONTEND_URL=http://localhost:5174`
- **Fix:** Updated `backend/main.py` CORS: added `http://localhost:5174` to `allow_origins`

### Fix 2: Excel Upload CORS Error
- **Problem:** `api/client.ts` used `BASE_URL = 'http://localhost:8000'` — absolute URLs bypassed the Vite proxy, hitting CORS restrictions
- **Fix:** Changed to `BASE_URL = ''` — all `/api` and `/ws` requests now use relative URLs routed through Vite's proxy
- **Fix:** WebSocket URL changed to `${proto}//${window.location.host}/ws/run/${runId}` (was hardcoded `ws://localhost:8000`)
- **Fix:** Improved error handling in `handleUpload` — shows actual backend `detail` message instead of generic error

### Fix 3: Upload Button Placeholder Text
- **Change:** Button text changed from `'Upload Pet_LandingPage.xlsx'` → `'Upload file.xlsx'`

### Fix 4: Remove Browser Version Dropdown
- **Change:** Removed `VERSIONS` constant and entire Browser Version `<Select>` from `RunTab.tsx`
- **Change:** Made `browser_version?: string` optional in `types/index.ts` with `always 'stable'` comment

---

## Session 3 — Anthropic API Authentication Fix

### Problem
Error: `"Could not resolve authentication method. Expected either api_key or auth_token to be set"`

### Root Causes
1. API key was shared publicly in conversation → likely auto-revoked by Anthropic
2. `thinking={"type": "enabled", "budget_tokens": 10000}` requires `betas=["interleaved-thinking-2025-05-14"]` header — not supported without it in SDK v0.84.0

### Fixes Applied
- **`config.py`:** Changed `.env` loading to absolute path:
  ```python
  _ENV_FILE = Path(__file__).resolve().parent / ".env"
  class Settings(BaseSettings):
      model_config = SettingsConfigDict(env_file=str(_ENV_FILE), ...)
  ```
- **`claude_orchestrator.py`:** Removed `thinking` param entirely, changed model to `claude-opus-4-5`, reduced `max_tokens=8000`
- **User action:** Regenerate API key at `https://console.anthropic.com/account/keys`

---

## Session 4 — Framework Path Corrections

### Fix 1: Wrong `FETCH_PATHS` in `framework_loader.py`
- **Problem:** Was fetching `src/fixtures`, `src/pages` — paths don't exist in repo
- **Fix:** Updated to correct paths with `skye-e2e-tests/` prefix:
  - `skye-e2e-tests/fixtures/`
  - `skye-e2e-tests/pages/`
  - `skye-e2e-tests/custom/`
  - `skye-e2e-tests/utils/`

### Fix 2: Wrong `PROJECT_MAP` in `execution_engine.py`
- **Problem:** Used invented names (`mobile-safari`, `tablet-safari`)
- **Fix:** Corrected to match `playwright.config.ts` ai-* projects:
  - `ai-chromium`, `ai-firefox`, `ai-webkit`, `ai-mobile-safari`, `ai-mobile-chrome`

### Fix 3: Wrong `PLAYWRIGHT_PROJECT_PATH`
- **Problem:** Path pointed to repo root instead of `skye-e2e-tests/` subfolder
- **Fix:** Updated `.env`: `PLAYWRIGHT_PROJECT_PATH=C:/Users/RajasekharUdumula/Desktop/QA_Automation_Banorte/skye-e2e-tests`

### Fix 4: `GENERATED_TESTS_DIR` Location
- **Problem:** `tests/generated/` directory didn't exist
- **Fix:** Created `C:\Users\RajasekharUdumula\Desktop\QA_Automation_Banorte\skye-e2e-tests\tests\generated\`

---

## Session 5 — Multi-LLM Provider Feature

### Feature: Switch between Anthropic Claude and Google Gemini

#### New File: `backend/llm_orchestrator.py`
- **Routes:** `stream_script()` → `_stream_anthropic()` or `_stream_gemini()` based on `provider` param
- **Shared:** Same `SYSTEM_PROMPT` and `FEW_SHOTS` for both providers
- **Anthropic format:** `role: "assistant"`, `content: "text"`
- **Gemini format:** `role: "model"`, `parts: ["text"]`
- **Lazy init:** `_get_anthropic()` and `_ensure_gemini()` — no crash if a key is missing
- **Usage tracking:** `stream_script.last_usage` stores provider, model, token counts
- **`active_provider_info()`** → returns config status for UI

#### Updated: `backend/config.py`
```python
LLM_PROVIDER: str = "anthropic"       # "anthropic" | "gemini"
ANTHROPIC_API_KEY: str = ""           # now optional
ANTHROPIC_MODEL: str = "claude-opus-4-5"
GEMINI_API_KEY: str = ""
GEMINI_MODEL: str = "gemini-2.5-pro"
```

#### Updated: `backend/requirements.txt`
```
anthropic>=0.49.0          # was ==0.34.0
google-generativeai>=0.8.0  # NEW
```
- Installed: `anthropic==0.84.0`, `google-generativeai==0.8.6`

#### Updated: `backend/main.py`
- Import changed: `from llm_orchestrator import stream_script, active_provider_info`
- New endpoint: `GET /api/llm-provider` → returns `active_provider_info()`
- `generate_script_endpoint` now accepts: `llm_provider: str = Form(default="")`
- Passes `provider=llm_provider.strip().lower() or None` to `stream_script()`
- `UserPrompt.model_used` reads from `usage.get("model", ...)` — tracks actual model used

#### Updated: `frontend/src/api/client.ts`
- Added `fetchLLMProvider()`: `GET /api/llm-provider`
- Added `llmProvider: string = ''` param to `createScriptStream()` — appended to FormData

#### Updated: `frontend/src/components/AIPhaseTab.tsx`
- Added `LLMProvider` type and `ProviderInfo` interface
- Added `provider` and `providerInfo` state
- `useEffect` fetches `/api/llm-provider` on mount → sets default provider from backend
- Added **"1. LLM Provider"** card with `Radio.Group`:
  - 🤖 Anthropic button (disabled if `ANTHROPIC_API_KEY` not set)
  - ✨ Gemini button (disabled if `GEMINI_API_KEY` not set)
- Shows active model name as colored `Tag` (purple=Anthropic, blue=Gemini)
- Shows ⚠ warning if selected provider's API key is not configured
- Step numbers shifted: Upload=2, Select Test Case=3, Extra Instructions=4
- `createScriptStream()` passes `provider` as last argument
- Streaming status: `▶ Streaming from {Gemini|Claude}…`

#### Bug Fix (same session): Duplicate `TextArea` Declaration
- **Problem:** `const { TextArea } = Input;` declared twice on lines 34 and 36
- **Fix:** Removed duplicate line (kept line 34)

#### Cleanup: Removed unused imports from `AIPhaseTab.tsx`
- Removed `Select`, `Divider` from antd imports
- Removed `type { UploadFile }` import

---

## Session 6 — Backend Restart with New Keys

### Actions
- User updated both `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` in `backend/.env`
- Killed old Python/uvicorn processes
- Restarted backend via PowerShell:
  ```powershell
  Start-Process -FilePath 'venv\Scripts\uvicorn.exe' `
    -ArgumentList 'main:app','--host','127.0.0.1','--port','8000','--reload' `
    -WorkingDirectory 'C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend'
  ```
- Health check confirmed: `{"status": "ok"}`
- Created `CLAUDE.md` (project memory for Claude Code)
- Created `memory.md` (this file — full changelog)

---

## Session 7 — Remove Validation Gate from Run Tab

### Problem
Generated scripts marked `validation_status = 'invalid'` by `tsc --noEmit` were not appearing in the "Select Generated Script" dropdown in RunTab.  The script was correctly generated and saved to the framework repo, but the frontend filter blocked it from being run.

### Root Cause
`frontend/src/components/RunTab.tsx` line 78 had:
```ts
const scripts = allScripts.filter((s) => s.validation_status === 'valid');
```
This silently excluded any script where TypeScript strict-mode emitted warnings (even false positives from framework-level types).  The backend `POST /api/run-test` never checked `validation_status` — only `file_path` and `typescript_code` presence — so the gate was frontend-only.

### Fix Applied
- **`RunTab.tsx`**: Removed the `.filter()` — `scripts` now equals `allScripts` (all generated scripts shown).
- Placeholder updated: `'No scripts yet — generate first'` / `'Choose a generated script…'`
- Dropdown tag colour: green = `valid`, orange = other statuses (informational only, not a gate).
- Selected-script info tag: shows `✓ valid` (green) or `⚠ tsc warnings` (orange) — run is always allowed.

### Key Rule Going Forward
TypeScript validation (`tsc --noEmit`) is **informational only**.  It must never block saving or running a generated script.  The `validation_status` field in the DB is a diagnostic hint, not an execution gate.

---

## 📊 Current State (as of last update)

| Service | Status | Port |
|---------|--------|------|
| Backend (FastAPI/uvicorn) | ✅ Running | 8000 |
| Frontend (Vite/React) | ✅ Running | 5174 |
| PostgreSQL | ✅ Running | 5432 |
| Redis | ✅ Running | 6379 |

| Package | Version |
|---------|---------|
| anthropic | 0.84.0 |
| google-generativeai | 0.8.6 |
| fastapi | 0.111.0 |
| uvicorn | 0.30.1 |

---

## Session 8 — Live Logs & GitHub Actions Fix

### Problems
1. **No live logs visible** in Run Testcase tab after clicking Run
2. **No GitHub Actions workflow** existed in the repo
3. **Race condition**: background task published Redis messages before WebSocket subscriber connected — all messages were silently lost
4. **No fallback**: if WebSocket missed logs, there was no recovery path

### Root Causes & Fixes

#### 1. Race condition — `github_actions_runner.py`
- Added `await asyncio.sleep(2)` before the first `pub()` call → gives the WebSocket client 2 s to connect and subscribe
- Changed `pub()` to also `RPUSH` every log line to Redis list `run:{run_id}:log_history` with 24 h TTL → messages persist for late subscribers

#### 2. History replay — `websocket_manager.py` (`redis_log_subscriber`)
- Subscribe to pub/sub FIRST (so new messages queue up)
- Then `LRANGE` the history list and replay all buffered messages to the client
- If `__DONE__` is already in history (run completed before client connected), return early — don't hang waiting for pub/sub messages that will never come

#### 3. HTTP fallback endpoint — `main.py`
- Added `GET /api/runs/{run_id}/logs` → returns Redis list as `{"lines": [...]}` JSON
- Used as fallback when WebSocket delivers nothing

#### 4. Frontend — `RunTab.tsx`
- Added `ghaUrl` state — extracts GitHub Actions run URL from log lines with regex
- Shows **"GitHub Actions Run ↗"** link in the Live Logs card header as soon as URL is detected
- Added HTTP polling fallback: if WebSocket delivers 0 lines in 6 s, starts polling `/api/runs/{run_id}/logs` every 3 s
- Poll stops when `__DONE__` is received

#### 5. GitHub Actions workflow — `QA_Automation_Banorte/.github/workflows/playwright.yml`
- **Created** `.github/workflows/playwright.yml` in the local `QA_Automation_Banorte` repo
- Triggers: `workflow_dispatch` (with inputs: `test_file`, `browser`, `environment`) + `push` to `ai-tests-staging` / `ai-generated-tests` branches
- Uses `ai-{browser}` Playwright projects (matching `playwright.config.ts`)
- **IMPORTANT**: Must be committed and pushed to `main` branch of the GitHub repo before GHA will trigger

### CRITICAL Action Required
The `.github/workflows/playwright.yml` workflow file must be **committed to the `main` branch** of `RajasekharPlay/QA_Automation_Banorte` on GitHub.  Run from `QA_Automation_Banorte/`:
```bash
git add .github/workflows/playwright.yml
git commit -m "ci: add Playwright workflow with workflow_dispatch trigger"
git push origin main
```
Also add secret `PW_HOST` in GitHub → Settings → Secrets & Variables → Actions → New repository secret.

### Import checking
All required imports (`test`, `expect`, page objects) are handled by safety nets in `main.py` → `_fix_import_paths`, `_fix_page_import_style`, `_ensure_imports_match_usage`. These run BEFORE the script is committed to GitHub.

---

## Session 9 — Critical DB Session Bug Fix (generate-script)

### Problem
All 25 generated scripts in DB had `typescript_code: ""`, `file_path: null`, `validation_status: "pending"`.
`POST /api/run-test` always rejected: `"Script has not been saved to the framework repo yet"` (HTTP 400).
No `ExecutionRun` records ever created → live logs blank → no GitHub Actions runs triggered.

### Root Cause — FastAPI StreamingResponse + dependency lifecycle mismatch
`get_db()` commits the session **when the route handler returns**, not when the streaming response finishes:
```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
        await session.commit()   # ← fires when generate_script_endpoint() RETURNS
```
`generate_script_endpoint` returns `StreamingResponse(event_stream())` immediately.
The session was committed & closed BEFORE `event_stream()` body ran.
The generator's `await db.flush()` operated on a closed session → changes silently discarded.

### Fix — `backend/main.py`
1. `from database import get_db, init_db, AsyncSessionLocal` — added `AsyncSessionLocal` import
2. Snapshotted `tc_parsed_json`, `tc_script_num`, `tc_module` from the TC object before returning StreamingResponse (session still valid at that point)
3. Inside `event_stream()` generator — replaced `await db.flush()` blocks with a dedicated `async with AsyncSessionLocal() as save_db:` block that:
   - Fetches the script record by ID
   - Sets `typescript_code`, `file_path`, `validation_status`, `validation_errors`
   - Adds `UserPrompt` audit record
   - Calls `await save_db.commit()` — explicit commit in dedicated session

### Rule: Never use the request `db` inside a StreamingResponse generator
The session is gone by the time the generator runs. Always open `AsyncSessionLocal()` inside the generator for DB writes.

---

## Session 10 — Multiple Stale Uvicorn Processes + Dropdown Filter Fix

### Problem
After deploying the Session 9 fix, scripts STILL had `file_path: null` and `typescript_code: ""`.
Diagnosis: `netstat -ano` showed **4 processes LISTENING on port 8000** — multiple stale uvicorn instances from previous restarts. Old processes (without the Session 9 fix) were handling generate-script requests. The new fixed process was never reached.

Also: the RunTab dropdown was showing all 26 stale scripts (all with `file_path: null`), making the user think they could run them — but they'd always get "Script has not been saved to the framework repo yet".

### Fix 1 — Kill all processes, start ONE clean instance
```powershell
Get-Process -Name 'python','python3','uvicorn' | Stop-Process -Force
# then start a single new uvicorn
```
Verified: only ONE PID listening on port 8000 after restart.

### Fix 2 — RunTab.tsx dropdown filter updated
- Changed from `const scripts = allScripts` (show everything)
- To `const scripts = allScripts.filter((s) => s.file_path != null && s.file_path !== '')`
- Stale scripts with `file_path: null` are hidden
- Only scripts that were FULLY generated (file saved to disk, DB committed) appear in the dropdown
- Placeholder: `'No ready scripts — go to AI Phase tab and generate one'`

### Rule: Always start uvicorn with `--reload` from a single terminal
Never run `Start-Process` multiple times without killing the previous process first.
Safest restart command:
```powershell
Get-Process -Name 'python','uvicorn' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2
Start-Process uvicorn.exe ... -WindowStyle Normal
```

---

## Session 11 — Race Condition #2 Fix: `asyncio.create_task()` before DB commit

### Problem
After Sessions 9 & 10 fixes, scripts now saved correctly (`file_path` populated, dropdown working).
But clicking Run Test still produced:
- **Zero output from `github_actions_runner`** — not even "Starting GitHub Actions runner…"
- **No GitHub Actions workflow runs** triggered
- **Live logs completely blank** (both WebSocket and HTTP polling returned empty)

The backend DID log:
```
INFO: POST /api/run-test HTTP/1.1 → 200 OK
```
And WebSocket DID connect successfully. But `_execute_and_update` was silently returning with no work done.

### Root Cause — `asyncio.create_task()` started before `get_db` committed

`run_test_endpoint` used the FastAPI `get_db` dependency:
```python
async def run_test_endpoint(... db: AsyncSession = Depends(get_db)):
    run = ExecutionRun(...)
    db.add(run)
    await db.flush()          # SQL INSERT sent, but NOT committed (still in transaction)
    run_id = str(run.id)

    asyncio.create_task(      # ← task starts IMMEDIATELY as a background coroutine
        _execute_and_update(run_id, ...)
    )
    return {"run_id": run_id}  # ← ONLY NOW does get_db commit the transaction
```

`get_db` commits **when the route handler returns**, not when `flush()` is called.
`asyncio.create_task()` schedules `_execute_and_update` to start immediately — potentially BEFORE `return` releases control back to `get_db`.

Inside `_execute_and_update`:
```python
async with AsyncSessionLocal() as db:
    run = await db.get(ExecutionRun, uuid.UUID(run_id))
    if not run:
        return   # ← SILENT EARLY EXIT — run record not committed yet → returns None
```

The `db.get()` opened a new connection and looked up the `run_id` UUID — which didn't exist in the DB yet (transaction not committed). So `run` was `None`, and the function returned silently, taking `github_actions_runner` with it. Nothing ever happened.

### Fix — `backend/main.py` in `run_test_endpoint`

Added explicit `await db.commit()` **before** `asyncio.create_task()`:
```python
db.add(run)
await db.flush()
run_id = str(run.id)

# ── CRITICAL: commit BEFORE spawning background task ──────────────────────
# _execute_and_update opens its own AsyncSessionLocal and does db.get(run_id).
# If the run record isn't visible (not committed), it returns None → silent exit
# → run_test_via_github_actions is never called → no logs, no GHA trigger.
await db.commit()

asyncio.create_task(
    _execute_and_update(run_id, ...)
)
return {"run_id": run_id, "status": "queued"}
```

### Rule: Always `await db.commit()` explicitly before `create_task()` when using `get_db`
`get_db`'s auto-commit fires at `return`. Background tasks spawned before `return` may read from a fresh DB connection that sees only committed data. Never rely on the teardown commit to happen before the task reads the record.

---

## Session 12 — GHA "Project ai-chromium not found" Fix

### Problem
GitHub Actions workflow failed with:
```
Error: Project(s) "ai-chromium" not found.
Available projects: "setup-auth", "setup-api", "setup-global", "Mobile Chrome"
```

### Root Cause — Two separate issues

#### Issue 1: `playwright.config.ts` never pushed to GitHub
The `ai-chromium` (and other `ai-*`) projects were added to the LOCAL
`playwright.config.ts` but never committed to Git.

**Fix:** Committed and pushed `skye-e2e-tests/playwright.config.ts` to `main`:
```bash
git add skye-e2e-tests/playwright.config.ts
git commit -m "ci: add ai-* Playwright projects for AI test automation platform"
git push origin main
```

#### Issue 2: `ai-tests-staging` uses OLD workflow YAML + OLD `playwright.config.ts`
`workflow_dispatch` with `ref: ai-tests-staging` causes GitHub to use the workflow
YAML from `ai-tests-staging` (old version) AND checkout `ai-tests-staging`
(has old `playwright.config.ts` without ai-* projects).

`ai-tests-staging` was forked from `main` BEFORE the config was updated → permanently
stale unless explicitly updated.

**Fix 1 — `playwright.yml` (on `main`):**
- Checkout step now hardcodes `ref: ai-tests-staging` + `fetch-depth: 0`
- Added "Sync playwright.config.ts from main" step:
  ```bash
  git fetch origin main --depth=1
  git checkout origin/main -- playwright.config.ts
  ```
- Removed `push` trigger (use `workflow_dispatch` only)

**Fix 2 — `github_actions_runner.py`:**
Changed trigger ref from `STAGING_BRANCH` to `"main"` so GHA uses the FIXED YAML:
```python
TRIGGER_BRANCH = "main"
await _trigger_workflow(client, workflow_id, TRIGGER_BRANCH, inputs)
conclusion, github_run_url = await _wait_for_run(
    client, workflow_id, TRIGGER_BRANCH, triggered_at, run_id, r
)
```
The YAML on `main` hardcodes `ref: ai-tests-staging` in checkout, so the spec file
is always found regardless of which ref triggered the dispatch.

### Final flow (after fix):
1. `github_actions_runner` commits spec to `ai-tests-staging`
2. Triggers `workflow_dispatch` on `main` → GHA uses FIXED YAML from `main`
3. YAML: checkout `ai-tests-staging` (spec file) + sync `playwright.config.ts` from `main` (ai-* projects)
4. `npx playwright test --project=ai-chromium` → ✅ project found, tests run

---

## Session 13 — Push-Trigger Re-Triggers Old Workflow + Log Streaming Fix

### Problem 1 — `ai-chromium not found` recurring (push trigger)
When we pushed the networkidle fix to `ai-tests-staging`, the OLD `playwright.yml`
on that branch had a `push:` trigger → GitHub triggered a run using the old workflow
(no playwright.config.ts sync, no ai-* projects). Also `test_file` was empty because
push-triggered runs don't pass workflow_dispatch inputs → "Running all generated tests"
with "ai-chromium not found".

**Fix:** Copied the updated `playwright.yml` from `main` to `ai-tests-staging` and pushed.
Now both branches have the same workflow YAML (no push trigger, sync step included).

### Problem 2 — In-progress GHA logs not showing in UI (HTTP fallback)
`_wait_for_run()` used `r.publish(channel, msg)` directly — bypassing the `pub()`
helper that also writes to `run:{run_id}:log_history` Redis list. HTTP polling fallback
calls `GET /api/runs/{run_id}/logs` which reads that list. So the HTTP fallback
(used when WebSocket falls back) was only seeing the initial messages, not the
every-10-second in-progress status updates.

**Fix — `backend/github_actions_runner.py`:**
- Refactored `_wait_for_run()` to accept `pub` as a parameter (async callable)
- All `r.publish()` calls inside replaced with `await pub()` — now go to BOTH pub/sub AND history list
- Poll interval reduced from 10s → 5s for faster UI updates
- Only publishes a status line when the status CHANGES (deduplication, avoids spam)

```python
# _wait_for_run now accepts pub callable
async def _wait_for_run(client, workflow_id, branch, triggered_after, pub, timeout_s=900):
    ...
    await pub(f"⏳ GHA status: {status_line} | elapsed={elapsed}s")  # ← uses pub(), not r.publish()

# Call site passes pub directly
conclusion, github_run_url = await _wait_for_run(
    client, workflow_id, TRIGGER_BRANCH, triggered_at, pub  # ← no more run_id/r args
)
```

### Problem 3 — `page.goto()` without networkidle causes "browser closed"
Banorte app does a JS redirect after the initial load event. Without `networkidle`,
the page context closes before Step 2 can interact.

**Fixes:**
- `llm_orchestrator.py` SYSTEM_PROMPT Rule 6: now MANDATES `{ waitUntil: 'networkidle' }`
- Both few-shot examples updated with networkidle
- Existing RB001 spec patched: `git push` to `ai-tests-staging` + DB UPDATE
- DB record updated via psql:
  ```sql
  UPDATE generated_scripts
  SET typescript_code = REPLACE(typescript_code,
    'await page.goto(process.env.pw_HOST!);',
    'await page.goto(process.env.pw_HOST!, { waitUntil: ''networkidle'' });')
  WHERE typescript_code LIKE '%page.goto(process.env.pw_HOST!)%';
  ```

---

## 🔜 Future Improvements (Not Yet Done)

- [ ] Self-correction loop: if `tsc --noEmit` fails, re-prompt LLM with error to fix it
- [ ] Run tab: stream live playwright output via WebSocket more reliably
- [ ] Dashboard: real-time run status polling
- [ ] Save generated script to Git (commit to framework repo)
- [ ] Support multiple Excel sheets in one upload
- [ ] Token usage tracking dashboard (compare Anthropic vs Gemini cost)
