"""
Claude Orchestrator
====================
Builds the full prompt (system + few-shots + framework context + test case JSON)
and calls claude-opus-4-6 with streaming.

The async generator `stream_script()` yields raw text chunks so the FastAPI
route can forward them straight to the client via Server-Sent Events.
After the stream is exhausted, `generate_script()` returns the full script.
"""
import json
import logging
from typing import AsyncGenerator
import anthropic
from config import settings
from framework_loader import get_framework_context

logger = logging.getLogger(__name__)

client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

# ════════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT  —  reflects the REAL skye-e2e-tests framework
# ════════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """
You are an expert Playwright/TypeScript test automation engineer who STRICTLY
follows the QA_Automation_Banorte (skye-e2e-tests) framework conventions.

CRITICAL RULES — never break these:

1. IMPORTS — always use these exact paths, never invent others:
     import { test }   from '../fixtures/Fixtures';
     import { expect } from '@playwright/test';
     // Only import a page class if it appears in the framework context supplied:
     import { PetsPage } from '../pages/PetsPage';
     import { MainPage } from '../pages/MainPage';
   NEVER import from '@playwright/test' for `test` — always use '../fixtures/Fixtures'.
   NEVER import 'allure-js-commons' or any allure package.

2. FIXTURE DESTRUCTURING — always exactly:
     async ({ page, skye, banorte }) => {
   • `page`    → standard Playwright Page object
   • `skye`    → SkyeAttributeCommands  (custom Skye UI interactions)
   • `banorte` → BanorteCommands        (Banorte-specific helpers)

3. PAGE OBJECT CONSTRUCTORS — use the correct signatures:
   • PetsPage : new PetsPage(page, skye)   ← TWO args required
   • MainPage : new MainPage(page)          ← ONE arg
   • Any other BasePage subclass that uses Skye: new XxxPage(page, skye)

4. STRUCTURE — wrap every logical step inside a test.step() block:
     await test.step('Step 1: Navigate to landing page', async () => {
       await page.goto(process.env.pw_HOST!);
     });

5. NAVIGATION — use process.env.pw_HOST! for the target base URL (public tests):
     await page.goto(process.env.pw_HOST!);
   For clicking into a product section use MainPage.selectProductByAlt(altText).

6. NO ALLURE — do NOT add allure.tag(), allure.story(), allure.description() calls.
   Allure is handled automatically by the allure-playwright reporter.

7. TEST NAMING — follow this exact pattern:
     test('RB001 - Short title matching the test_case_name', async ({ page, skye, banorte }) => {

8. ASSERTIONS — always use expect() from @playwright/test; never console.log.

9. OUTPUT — return ONLY the TypeScript file content.
   No markdown code fences, no explanations, no comments outside the code.

10. TYPE SAFETY — the file must pass `tsc --noEmit --skipLibCheck`.
    Use correct types; no implicit `any`.

11. FRAMEWORK HELPERS — when skye or banorte methods match the action, prefer them
    over raw Playwright calls. For example:
      skye.clickOnUiTab('Coberturas base')  instead of  page.click('.tab')
      banorte.handlePopupAceptar()          for pop-up dismissal
"""

# ════════════════════════════════════════════════════════════════════════════════
# FEW-SHOT EXAMPLES
# Based on REAL skye-e2e-tests patterns (RB001 + RB007)
# ════════════════════════════════════════════════════════════════════════════════
FEW_SHOTS = [
    {
        "user": """Framework context: [truncated for brevity — full context provided in real calls]

Test Case JSON:
{
  "test_script_num": "RB001",
  "module": "RB_Pets_Landing_Page",
  "test_case_name": "Verify Pet landing page with newly added 3 tabs from public page",
  "description": "Verify user sees 3 tabs: a)Coberturas base b)Servicios opcionales c)Exclusiones",
  "steps": [
    {"step_no": 1, "action": "Navigate to the application URL", "input_data": ""},
    {"step_no": 2, "action": "Click on Mascotas menu item", "input_data": ""},
    {"step_no": 3, "action": "Click Ver seguro button", "input_data": ""},
    {"step_no": 4, "action": "Verify 3 tabs are visible: Coberturas base, Servicios opcionales, Exclusiones", "input_data": ""}
  ],
  "expected_results": "User should see 3 tabs: Coberturas base, Servicios opcionales, Exclusiones"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../fixtures/Fixtures';
import { expect } from '@playwright/test';
import { MainPage } from '../pages/MainPage';
import { PetsPage } from '../pages/PetsPage';

test('RB001 - Verify Pet landing page with newly added 3 tabs from public page',
  async ({ page, skye, banorte }) => {

    const mainPage = new MainPage(page);
    const petsPage = new PetsPage(page, skye);

    await test.step('Step 1: Navigate to the application URL', async () => {
      await page.goto(process.env.pw_HOST!);
    });

    await test.step('Step 2: Click on Mascotas menu item', async () => {
      await mainPage.selectProductByAlt('Mascotas');
    });

    await test.step('Step 3: Click Ver seguro button', async () => {
      await petsPage.clickOnButton('Ver seguro');
    });

    await test.step('Step 4: Verify 3 tabs are visible', async () => {
      await expect(page.getByRole('tab', { name: 'Coberturas base' })).toBeVisible();
      await expect(page.getByRole('tab', { name: 'Servicios opcionales' })).toBeVisible();
      await expect(page.getByRole('tab', { name: 'Exclusiones' })).toBeVisible();
    });
});""",
    },
    {
        "user": """Framework context: [truncated]

Test Case JSON:
{
  "test_script_num": "RB007",
  "module": "RB_Pets_Landing_Page",
  "test_case_name": "Verify phone link is clickable",
  "description": "Verify user can click phone link on the Pets landing page",
  "steps": [
    {"step_no": 1, "action": "Navigate to the application URL", "input_data": ""},
    {"step_no": 2, "action": "Click on Mascotas menu item", "input_data": ""},
    {"step_no": 3, "action": "Click on the phone number link", "input_data": ""}
  ],
  "expected_results": "User is redirected to initiate a call (on mobile) or opens dialer"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../fixtures/Fixtures';
import { expect } from '@playwright/test';
import { MainPage } from '../pages/MainPage';

test('RB007 - Verify phone link is clickable', async ({ page, skye, banorte }) => {

    const mainPage = new MainPage(page);

    await test.step('Step 1: Navigate to the application URL', async () => {
      await page.goto(process.env.pw_HOST!);
    });

    await test.step('Step 2: Click on Mascotas menu item', async () => {
      await mainPage.selectProductByAlt('Mascotas');
    });

    await test.step('Step 3: Verify phone link is present and has tel: href', async () => {
      const phoneLink = page.locator('a[href^="tel:"]').first();
      await expect(phoneLink).toBeVisible();
      const href = await phoneLink.getAttribute('href');
      expect(href).toMatch(/^tel:\+?[\d\s\-()+]+$/);
    });
});""",
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# PROMPT BUILDER
# ════════════════════════════════════════════════════════════════════════════════

def _build_messages(
    test_case_json: dict,
    user_instruction: str,
    framework_context: str,
) -> list[dict]:
    messages: list[dict] = []

    # Inject few-shot examples
    for shot in FEW_SHOTS:
        messages.append({"role": "user", "content": shot["user"]})
        messages.append({"role": "assistant", "content": shot["assistant"]})

    # Main request
    main_content = f"""Framework context (existing Page Objects, fixtures, custom commands):
```typescript
{framework_context}
```

Test Case JSON:
```json
{json.dumps(test_case_json, indent=2)}
```

User instruction: {user_instruction.strip() or 'Generate a Playwright/TypeScript test following the framework conventions strictly.'}

Generate the complete TypeScript test file now:"""

    messages.append({"role": "user", "content": main_content})
    return messages


# ════════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════════════════════

async def stream_script(
    test_case_json: dict,
    user_instruction: str,
    framework_context: str,
) -> AsyncGenerator[str, None]:
    """
    Async generator — yields text chunks as Claude produces them.
    Caller is responsible for collecting the full script.

    Usage:
        full = ""
        async for chunk in stream_script(tc, prompt, ctx):
            full += chunk
            await sse.send(chunk)   # forward to frontend
    """
    messages = _build_messages(test_case_json, user_instruction, framework_context)

    async with client.messages.stream(
        model="claude-opus-4-5",           # use a verified model identifier
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text

        # Capture usage for audit after stream ends
        final = await stream.get_final_message()
        usage = final.usage
        logger.info(
            "Claude usage — input: %d, output: %d tokens",
            usage.input_tokens,
            usage.output_tokens,
        )
        # Attach to generator so caller can inspect if needed
        stream_script.last_usage = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }


async def generate_script(
    test_case_json: dict,
    user_instruction: str = "",
    force_framework_refresh: bool = False,
) -> tuple[str, str, dict]:
    """
    Non-streaming wrapper — returns (full_script, framework_context_hash, usage).
    Use this when you don't need SSE streaming.
    """
    ctx, ctx_hash = get_framework_context(force_refresh=force_framework_refresh)
    full_script = ""
    async for chunk in stream_script(test_case_json, user_instruction, ctx):
        full_script += chunk

    usage = getattr(stream_script, "last_usage", {})
    return full_script, ctx_hash, usage
