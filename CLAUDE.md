# CLAUDE.md — AI Test Automation Platform
> This file is read by Claude Code at the start of every session.
> It contains the full project context, architecture, all fixes applied, and how to start services.

---

## 📌 Project Overview

**Name:** AI Test Automation Platform
**Location:** `C:\Users\RajasekharUdumula\Desktop\ai-test-platform\`
**Purpose:** Upload Excel test cases → AI generates Playwright/TypeScript scripts → Execute tests → View Allure reports
**Framework target:** `QA_Automation_Banorte` / `skye-e2e-tests` (Banorte insurance company)

---

## 🗂 Project Structure

```
ai-test-platform/
├── backend/                         # FastAPI Python backend
│   ├── main.py                      # All API routes
│   ├── config.py                    # Settings loaded from .env (absolute path)
│   ├── database.py                  # SQLAlchemy async + AsyncSessionLocal
│   ├── models.py                    # TestCase, GeneratedScript, ExecutionRun, UserPrompt
│   ├── excel_parser.py              # Parses .xlsx → TestCase objects
│   ├── framework_loader.py          # GitHub API → fetches skye-e2e-tests/ files → Redis cache
│   ├── llm_orchestrator.py          # Multi-provider: Anthropic Claude + Google Gemini
│   ├── claude_orchestrator.py       # Legacy (superseded by llm_orchestrator.py)
│   ├── script_validator.py          # tsc --noEmit validation + self-correction
│   ├── execution_engine.py          # npx playwright test subprocess + Allure
│   ├── websocket_manager.py         # WebSocket + Redis pub/sub bridge
│   ├── requirements.txt             # Python dependencies
│   ├── .env                         # Real secrets (NOT committed)
│   ├── .env.example                 # Template
│   └── venv/                        # Python virtual environment (use venv/ NOT .venv/)
│
├── frontend/                        # React + TypeScript + Ant Design + Vite
│   ├── src/
│   │   ├── App.tsx                  # 3-tab dark layout: AI Phase / Run Testcase / Dashboard
│   │   ├── api/client.ts            # All API calls (relative URLs via Vite proxy)
│   │   ├── types/index.ts           # TypeScript interfaces
│   │   └── components/
│   │       ├── AIPhaseTab.tsx       # LLM provider toggle + upload + generate + Monaco editor
│   │       ├── RunTab.tsx           # Script select + env/browser config + live logs
│   │       └── Dashboard.tsx        # Stats, pie chart, run history, Allure embed
│   ├── vite.config.ts               # Port 5174 (strictPort), proxy /api → :8000, /ws → ws://:8000
│   └── package.json
│
├── CLAUDE.md                        # ← THIS FILE (Claude reads on session start)
├── memory.md                        # Full changelog of all changes made
└── README.md                        # Setup guide
```

---

## 🚀 How to Start Services

### Backend (FastAPI on port 8000)
```powershell
# Method 1 — PowerShell (recommended, opens new window)
Start-Process -FilePath 'C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend\venv\Scripts\uvicorn.exe' `
  -ArgumentList 'main:app','--host','127.0.0.1','--port','8000','--reload' `
  -WorkingDirectory 'C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend' `
  -WindowStyle Normal

# Method 2 — CMD window (run from backend/ directory)
cd C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend
venv\Scripts\uvicorn.exe main:app --host 127.0.0.1 --port 8000 --reload
```

### Frontend (Vite on port 5174)
```bash
cd C:\Users\RajasekharUdumula\Desktop\ai-test-platform\frontend
npm run dev
```

### Health check
```
GET http://127.0.0.1:8000/health  → {"status": "ok"}
UI: http://localhost:5174
API docs: http://127.0.0.1:8000/docs
```

---

## ⚙️ Environment Variables (.env)

File: `C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend\.env`

```env
# LLM Provider — "anthropic" or "gemini"
LLM_PROVIDER=anthropic

# Anthropic Claude
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx          # Get from console.anthropic.com
ANTHROPIC_MODEL=claude-opus-4-5

# Google Gemini
GEMINI_API_KEY=AIzaSyxxxxx                     # Get from aistudio.google.com
GEMINI_MODEL=gemini-2.5-pro

# GitHub (for fetching framework context)
GITHUB_TOKEN=ghp_xxxxx
GITHUB_FRAMEWORK_REPO=RajasekharPlay/QA_Automation_Banorte

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://postgres:Sreeram@localhost:5432/ai_test_platform
SYNC_DATABASE_URL=postgresql://postgres:Sreeram@localhost:5432/ai_test_platform

# Redis
REDIS_URL=redis://localhost:6379/0

# Framework Playwright project path (skye-e2e-tests SUBFOLDER — not repo root)
PLAYWRIGHT_PROJECT_PATH=C:/Users/RajasekharUdumula/Desktop/QA_Automation_Banorte/skye-e2e-tests
GENERATED_TESTS_DIR=tests/generated

# App
FRONTEND_URL=http://localhost:5174
SECRET_KEY=banorte-ai-platform-secret-2024
```

> ⚠️ CRITICAL: `PLAYWRIGHT_PROJECT_PATH` must point to the `skye-e2e-tests/` **subfolder**, NOT the repo root.
> ⚠️ CRITICAL: `config.py` uses `Path(__file__).resolve().parent / ".env"` — absolute path, no CWD dependency.

---

## 🤖 LLM Orchestrator — Multi-Provider

**File:** `backend/llm_orchestrator.py`

- **Anthropic Claude**: `claude-opus-4-5`, max_tokens=8000, SSE streaming via `messages.stream()`
- **Google Gemini**: `gemini-2.5-pro`, streaming via `send_message_async(stream=True)`
- Both share the same `SYSTEM_PROMPT` and `FEW_SHOTS` examples
- Per-request provider override via `llm_provider` Form field → overrides `.env` default
- Lazy client init: `_get_anthropic()` and `_ensure_gemini()` — safe to have only one key configured
- `active_provider_info()` → used by `GET /api/llm-provider` endpoint

**Message format differences:**
| | Anthropic | Gemini |
|---|---|---|
| AI role | `"assistant"` | `"model"` |
| Content | `"content": "text"` | `"parts": ["text"]` |

---

## 🌐 API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/api/parse-excel` | Upload .xlsx → returns test_cases[] |
| GET | `/api/test-cases` | List all test cases from DB |
| GET | `/api/llm-provider` | Returns provider config & which keys are set |
| POST | `/api/generate-script` | SSE stream: generates TypeScript from test case |
| GET | `/api/scripts` | List all generated scripts |
| GET | `/api/scripts/{id}` | Single script detail |
| POST | `/api/run-test` | Enqueue test execution → returns run_id |
| GET | `/api/runs` | List all execution runs |
| GET | `/api/runs/{id}` | Single run detail |
| GET | `/api/reports/{id}` | Serve Allure HTML report |
| POST | `/api/framework/refresh` | Re-fetch framework from GitHub |
| WS | `/ws/run/{run_id}` | Live log stream |

---

## 🎭 Framework — skye-e2e-tests Conventions

**CRITICAL** — The LLM is instructed to follow these EXACT patterns:

```typescript
// Imports — ALWAYS these exact paths
import { test }   from '../fixtures/Fixtures';
import { expect } from '@playwright/test';
import { PetsPage } from '../pages/PetsPage';  // only if used
import { MainPage } from '../pages/MainPage';   // only if used

// Fixture destructuring — ALWAYS exactly this
async ({ page, skye, banorte }) => {
  // page    → Playwright Page
  // skye    → SkyeAttributeCommands
  // banorte → BanorteCommands

// Page object constructors
new PetsPage(page, skye)   // TWO args
new MainPage(page)          // ONE arg

// Navigation
await page.goto(process.env.pw_HOST!);

// Steps — every logical step wrapped
await test.step('Step 1: Navigate', async () => { ... });

// Assertions
await expect(locator).toBeVisible();

// No allure imports, no markdown fences in output
```

---

## 📁 Framework GitHub Paths Fetched

**Repo:** `RajasekharPlay/QA_Automation_Banorte`
**Paths fetched** (in `framework_loader.py`):
- `skye-e2e-tests/fixtures/`
- `skye-e2e-tests/pages/`
- `skye-e2e-tests/custom/`
- `skye-e2e-tests/utils/`

---

## 🎯 Playwright Projects (ai-test-platform generated tests)

Added to `skye-e2e-tests/playwright.config.ts` — no auth dependencies:
- `ai-chromium`
- `ai-firefox`
- `ai-webkit`
- `ai-mobile-safari`
- `ai-mobile-chrome`

**Generated tests saved to:**
`C:\Users\RajasekharUdumula\Desktop\QA_Automation_Banorte\skye-e2e-tests\tests\generated\`

---

## ⚠️ Critical Config Fix — `env_ignore_empty=True`

Windows had `ANTHROPIC_API_KEY=""` set as an OS environment variable (empty string).
Pydantic-settings gives OS env vars priority over `.env` values — so the key read as empty.

**Fix applied in `config.py`:**
```python
model_config = SettingsConfigDict(
    env_file=str(_ENV_FILE),
    env_file_encoding="utf-8",
    env_ignore_empty=True,   # ← ignores OS vars set to "" so .env values win
)
```
Without this, any OS-level empty env var silently overrides the `.env` file.

---

## 🔑 Key Technical Decisions

| Decision | Reason |
|----------|--------|
| `BASE_URL = ''` in client.ts | Relative URLs go through Vite proxy → no CORS |
| Vite port `5174` with `strictPort: true` | Port 5173 occupied by another app |
| `Path(__file__).resolve().parent / ".env"` | Avoids CWD-relative .env failure |
| Lazy LLM client init | Safe when only one provider key is configured |
| Removed `thinking` param from Anthropic | SDK v0.84.0 requires `betas` header for thinking |
| `venv/` (not `.venv/`) | That's where pip installed packages |

---

## 🐛 Known Fixes Applied

See `memory.md` for full chronological changelog.

Quick reference:
1. Excel upload CORS error → `BASE_URL = ''` (relative URLs)
2. Vite port conflict → `strictPort: true`, port 5174
3. Anthropic auth error → absolute `.env` path, removed `thinking` param
4. Wrong framework paths → fixed to `skye-e2e-tests/` prefix
5. Wrong playwright projects → fixed to `ai-chromium`, `ai-firefox`, etc.
6. Duplicate `TextArea` declaration → removed duplicate
7. Multi-LLM provider → `llm_orchestrator.py` with Anthropic + Gemini routing
