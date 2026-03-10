from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to THIS file (backend/.env), not the process CWD.
# This is important when uvicorn is started from a different directory.
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,   # ← ignore OS env vars set to "" so .env file wins
    )

    # ── LLM Provider ─────────────────────────────────────────────────────────
    # Set to "anthropic" or "gemini". Used as the default when no provider is
    # specified per-request. Individual requests can override via form param.
    LLM_PROVIDER: str = "anthropic"

    # Anthropic (Claude)
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-4-5"

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-pro"

    # ── GitHub (Private framework repo) ──────────────────────────────────────
    GITHUB_TOKEN: str
    GITHUB_FRAMEWORK_REPO: str = "RajasekharPlay/QA_Automation_Banorte"

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_test_platform"
    SYNC_DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/ai_test_platform"

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Playwright ────────────────────────────────────────────────────────────
    PLAYWRIGHT_PROJECT_PATH: str
    GENERATED_TESTS_DIR: str = "tests/generated"

    # ── App ───────────────────────────────────────────────────────────────────
    FRONTEND_URL: str = "http://localhost:5174"
    SECRET_KEY: str = "change-me"


settings = Settings()
