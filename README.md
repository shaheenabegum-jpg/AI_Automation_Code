# AI Test Automation Platform — Setup Guide

## Prerequisites
- Python 3.11+
- Node.js 20+
- PostgreSQL 15+ (running locally, DB: `ai_test_platform`)
- Redis (running locally on port 6379)
- Allure CLI: `npm install -g allure-commandline`

---

## 1. Configure `.env`

Edit `backend/.env`:
```env
LLM_PROVIDER=anthropic            # "anthropic" or "gemini"
ANTHROPIC_API_KEY=sk-ant-api03-xxxx   # console.anthropic.com
ANTHROPIC_MODEL=claude-opus-4-5
GEMINI_API_KEY=AIzaSyxxxx             # aistudio.google.com
GEMINI_MODEL=gemini-2.5-pro

GITHUB_TOKEN=ghp_xxxx
GITHUB_FRAMEWORK_REPO=RajasekharPlay/QA_Automation_Banorte

DATABASE_URL=postgresql+asyncpg://postgres:Sreeram@localhost:5432/ai_test_platform
SYNC_DATABASE_URL=postgresql://postgres:Sreeram@localhost:5432/ai_test_platform
REDIS_URL=redis://localhost:6379/0

# ⚠️ Must point to skye-e2e-tests SUBFOLDER (not repo root)
PLAYWRIGHT_PROJECT_PATH=C:/Users/RajasekharUdumula/Desktop/QA_Automation_Banorte/skye-e2e-tests
GENERATED_TESTS_DIR=tests/generated

FRONTEND_URL=http://localhost:5174
SECRET_KEY=banorte-ai-platform-secret-2024
```

---

## 2. PostgreSQL — create database

```bash
psql -U postgres
CREATE DATABASE ai_test_platform;
\q
```

---

## 3. Redis — start

```bash
redis-server
```

---

## 4. Backend

```powershell
# Install dependencies
cd C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend
venv\Scripts\pip.exe install -r requirements.txt

# Start server (opens new window)
Start-Process -FilePath 'venv\Scripts\uvicorn.exe' `
  -ArgumentList 'main:app','--host','127.0.0.1','--port','8000','--reload' `
  -WorkingDirectory 'C:\Users\RajasekharUdumula\Desktop\ai-test-platform\backend' `
  -WindowStyle Normal
```

API docs: http://localhost:8000/docs

---

## 5. Frontend

```bash
cd C:\Users\RajasekharUdumula\Desktop\ai-test-platform\frontend
npm install
npm run dev
```

UI: http://localhost:5174

---

## 6. Workflow

### AI Phase Tab
1. **Choose LLM Provider** — toggle between 🤖 Anthropic Claude and ✨ Gemini
2. **Upload Excel** — upload your `.xlsx` test case file
3. **Select Test Case** — pick from the parsed list
4. **Extra Instructions** (optional) — add custom guidance
5. Click **Generate Script** — LLM streams TypeScript live in Monaco editor
6. Script is auto-validated with `tsc --noEmit` and saved to framework repo

### Run Testcase Tab
1. Select generated script
2. Configure: Environment / Browser / Device / Mode / Tags
3. Click **Run Test**
4. Watch live logs stream
5. Click **Open** to view Allure report

### Dashboard
- Pass/fail pie chart
- Execution history with Allure report links

---

## File Structure

```
ai-test-platform/
├── backend/
│   ├── main.py                # FastAPI + all routes
│   ├── config.py              # Settings (absolute .env path)
│   ├── database.py            # SQLAlchemy async
│   ├── models.py              # DB models
│   ├── excel_parser.py        # .xlsx → TestCase
│   ├── framework_loader.py    # GitHub → Redis cache
│   ├── llm_orchestrator.py    # Anthropic + Gemini routing ← ACTIVE
│   ├── claude_orchestrator.py # Legacy (superseded)
│   ├── script_validator.py    # tsc --noEmit validation
│   ├── execution_engine.py    # playwright test + Allure
│   ├── websocket_manager.py   # WebSocket + Redis pub/sub
│   ├── requirements.txt
│   └── .env
├── frontend/
│   ├── src/
│   │   ├── App.tsx            # 3-tab dark layout
│   │   ├── api/client.ts      # API + SSE + WebSocket
│   │   ├── types/index.ts     # TypeScript interfaces
│   │   └── components/
│   │       ├── AIPhaseTab.tsx # LLM toggle + upload + generate
│   │       ├── RunTab.tsx     # Execute + live logs
│   │       └── Dashboard.tsx  # Stats + charts + Allure
│   ├── vite.config.ts         # Port 5174, proxy /api & /ws
│   └── package.json
├── CLAUDE.md                  # Claude Code memory file
├── memory.md                  # Full changelog
└── README.md                  # This file
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `tsc not found` | `npm install -g typescript` in framework repo dir |
| Redis connection refused | Start Redis: `redis-server` |
| GitHub 404 on framework | Check `GITHUB_TOKEN` in `.env` has repo read access |
| Allure not generating | `npm install -g allure-commandline` |
| WebSocket not connecting | Check `FRONTEND_URL` in `.env` matches your frontend port |
| Gemini button disabled | Set `GEMINI_API_KEY` in `.env` and restart backend |
| Anthropic button disabled | Set `ANTHROPIC_API_KEY` in `.env` and restart backend |
| Backend can't find `.env` | Path is absolute: `config.py` uses `Path(__file__).resolve().parent / ".env"` |
