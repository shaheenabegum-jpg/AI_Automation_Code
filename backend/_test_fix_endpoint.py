"""Quick test of fix-script endpoint logic."""
import asyncio
import sys
sys.path.insert(0, ".")

async def main():
    from database import AsyncSessionLocal
    from models import ExecutionRun
    import uuid

    run_id = "41f88585-9015-41cc-a83a-f3d47b8a4981"

    async with AsyncSessionLocal() as db:
        run = await db.get(ExecutionRun, uuid.UUID(run_id))
        if not run:
            print("Run not found")
            return
        print(f"Run found: status={run.status}, script_id={run.script_id}")
        print(f"spec_file_path={run.spec_file_path}")

        # Check status comparison
        run_status_str = run.status.value if hasattr(run.status, 'value') else str(run.status)
        print(f"Status string: '{run_status_str}'")
        print(f"Is failed: {run_status_str in ('failed', 'error')}")

        # Try to get script code
        from pathlib import Path
        from config import settings
        original_code = ""
        if run.spec_file_path:
            for base in [settings.PLAYWRIGHT_PROJECT_PATH, settings.MGA_PLAYWRIGHT_PROJECT_PATH]:
                if not base:
                    continue
                spec_path = Path(base) / run.spec_file_path
                alt_path = Path(base) / run.spec_file_path.removeprefix("skye-e2e-tests/")
                for p in [spec_path, alt_path]:
                    print(f"  Checking: {p} exists={p.exists()}")
                    if p.exists():
                        original_code = p.read_text(encoding="utf-8")
                        break
                if original_code:
                    break

        print(f"Original code length: {len(original_code)}")
        if not original_code:
            print("ERROR: Could not find original script code!")

asyncio.run(main())
