"""
LLM Orchestrator — multi-provider script generation
====================================================
Supported providers:
  • anthropic  — Claude models  (set ANTHROPIC_MODEL in .env)
  • gemini     — Google Gemini  (set GEMINI_MODEL in .env)

Active provider is determined by:
  1. `provider` argument passed to stream_script() at call time  ← per-request
  2. Falls back to settings.LLM_PROVIDER from .env              ← global default

Add both keys to .env; only the active provider's key is used.
"""
import json
import logging
from typing import AsyncGenerator, Literal

import anthropic
import google.generativeai as genai

from config import settings
from framework_loader import get_framework_context

logger = logging.getLogger(__name__)

LLMProvider = Literal["anthropic", "gemini"]

# ── Lazy client / config holders ─────────────────────────────────────────────
_anthropic_client: anthropic.AsyncAnthropic | None = None
_gemini_configured: bool = False


def _get_anthropic() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY
        )
    return _anthropic_client


def _ensure_gemini() -> None:
    global _gemini_configured
    if not _gemini_configured:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        _gemini_configured = True


# ════════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT  (shared across providers)
# ════════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """
You are an expert Playwright/TypeScript test automation engineer who STRICTLY
follows the QA_Automation_Banorte (skye-e2e-tests) framework conventions.

CRITICAL RULES — never break these:

1. IMPORTS — two rules that MUST both be followed:

   a) PATH DEPTH — scripts live in tests/generated/ (TWO levels below root).
      Always use ../../ (two levels up), NEVER ../ (one level up).

   b) IMPORT STYLE — Fixtures.ts uses named exports; page/custom classes use DEFAULT exports.
      Named exports  → import WITH braces  { }
      Default exports → import WITHOUT braces (no curly brackets)

   CORRECT IMPORT PATTERN (copy this exactly):
     import { test }   from '../../fixtures/Fixtures';   ← named export  — WITH braces
     import { expect } from '@playwright/test';           ← named export  — WITH braces
     import PetsPage   from '../../pages/PetsPage';       ← DEFAULT export — NO braces
     import MainPage   from '../../pages/MainPage';       ← DEFAULT export — NO braces

   NEVER write:  import { PetsPage } from '../../pages/PetsPage';   ← WRONG
   NEVER write:  import { MainPage } from '../../pages/MainPage';   ← WRONG
   NEVER write:  import { test } from '@playwright/test';           ← WRONG (use Fixtures)
   NEVER write:  '../fixtures/...' or '../pages/...'                ← WRONG depth
   NEVER import 'allure-js-commons' or any allure package.
   NEVER import SkyeAttributeCommands or BanorteCommands — they come from the fixture.

   RULE: Only import a page class if you actually instantiate it with `new`.
     import MainPage from '../../pages/MainPage';   ← only if you write  new MainPage(page)
     import PetsPage from '../../pages/PetsPage';   ← only if you write  new PetsPage(page, skye)

2. FIXTURE DESTRUCTURING — always exactly:
     async ({ page, skye, banorte }) => {
   • `page`    → standard Playwright Page object
   • `skye`    → SkyeAttributeCommands instance  (do NOT import this class)
   • `banorte` → BanorteCommands instance         (do NOT import this class)

3. PAGE OBJECT CONSTRUCTORS — use the correct signatures:
   • new PetsPage(page, skye)   ← TWO args: Page + SkyeAttributeCommands
   • new MainPage(page)          ← ONE arg: Page only

4. AVAILABLE METHODS — only call methods that are listed here. Never invent method names.

   MainPage methods:
     mainPage.goto()                        → navigate to the private app URL
     mainPage.selectProductByAlt(altText)   → click insurance product image by alt text (string)
     mainPage.isLoaded()                    → Promise<boolean> — check page loaded
     mainPage.clickOnButton(buttonName)     → click <button> by visible name (string)
     mainPage.clickOnLink(linkName)         → click <a> by visible name (string)

   PetsPage methods (no own methods — only inherits from BasePage):
     petsPage.clickOnButton(buttonName)     → click <button> by visible name (string)
     petsPage.clickOnLink(linkName)         → click <a> by visible name (string)

   skye methods (SkyeAttributeCommands — use via `skye`, no import needed):
     skye.selectFromDropdown(testId, optionIndex, expectedValue)
     skye.selectRadioOptionByTestId(testId)
     skye.fillDateToDatepicker(testId, inputDate)          ← inputDate format: 'ddMMyyyy'
     skye.fillDateMaskedTextbox(testId, inputDate)
     skye.fillGenericTextbox(testId, inputValue, expectedOutputValue?)
     skye.clickOnActionButton(buttonId, expectValidationErrors?)
     skye.clickOnUiTab(testId)
     skye.clickOnUiPanel(id)
     skye.verifyProcessStepIsLoaded(processStepId)
     skye.verifyGenericTextboxValue(testId, expectedValue)
     skye.verifyMessageAttributeContent(testId, expectedMessageContent)
     skye.verifyPopupIsLoaded(skTestId)
     skye.closePopup()
     skye.selectFromPackageSelector(testId, searchedLabel)
     skye.verifyValidationMessage(testId, message, expectValidationErrors)
     skye.verifyHtmlJsAttributeContent(skTestId, expectedMessageContent)
     skye.setSliderValue(containerTestId, inputValue)
     skye.verifySliderRanges(testId, valueMin, valueMax)
     skye.verifyLinkAttribute(skTestId, expectedLabel)
     skye.verifyCounterValue(testId, expectedValueRegexp, expectedValue?)
     skye.selectFromCheckbox(testId)
     skye.addMultibrickWithButton(buttonLabel, newMultibrickTestId)
     skye.removeMultibrickWithButton(multibrickTestId)
     skye.verifyNumberOfMultibricks(testId, expectedNrOfMultibricks)
     skye.selectFromFlexDataSearch(testId, inputValue, expectedValue)
     skye.selectFromFlexDataDropdown(skTestId, inputValue, expectedValueSavedToDb)
     skye.uploadToResource(skTestId, filePaths)
     skye.verifyNoLoadingIcon(skTestId)
     skye.extractImplementationUuidBasedOnProcessId(processId)
     skye.fillRichTextArea(testId, inputValue, validateText)

   banorte methods (BanorteCommands — use via `banorte`, no import needed):
     banorte.handlePopupAceptar(skTestId, processName, buttonTitle, isThereACheckbox?, checkboxTestId?)
     banorte.selectFromRangeDates(testId, dateFrom, dateTo)  ← dates: 'dd.MM.yyyy'
     banorte.increaseNumberInTextbox(testId)
     banorte.getPayment(classId)  → Promise<Number>
     banorte.checkToggle(dataTestId)

   For ANY UI action not covered by the above, use Playwright built-ins directly:
     page.locator(selector)
     page.getByRole(role, { name })
     page.getByText(text)
     page.getByTestId(testId)
     page.getByLabel(label)
     page.fill(selector, value)
     page.click(selector)

5. STRUCTURE — wrap every logical step inside a test.step() block.

6. NAVIGATION — ALWAYS use this EXACT two-line pattern inside Step 1:

     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('<FIRST_INTERACTIVE_ELEMENT_SELECTOR>', { state: 'visible', timeout: 30000 });

   Rules:
   a) Use `waitUntil: 'domcontentloaded'` — NEVER 'networkidle' (hangs on SPAs)
      and NEVER plain page.goto() without waitUntil.
   b) Immediately after goto(), add page.waitForSelector() with BOTH { state: 'visible', timeout: 30000 }
      for the FIRST element the test will interact with (e.g., a nav menu, a heading, a button).
      This waits for JS redirects to complete and the SPA to finish rendering.
      The explicit timeout: 30000 is required — CI runners are cold and need extra time.
   c) The selector in waitForSelector MUST match what Step 2 will click/verify.

   Example — if Step 2 clicks the nav menu (data-testid="sk-nav-menu"):
     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('[data-testid="sk-nav-menu"]', { state: 'visible', timeout: 30000 });

   Example — if Step 2 clicks img[alt="Mascotas"]:
     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('img[alt="Mascotas"]', { state: 'visible', timeout: 30000 });

   Example — if Step 2 verifies a heading:
     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('h1', { state: 'visible', timeout: 30000 });

7. NO ALLURE — do NOT add allure.tag(), allure.story(), or similar calls.

8. TEST NAMING — follow this exact pattern:
     test('RB001 - Short title matching the test_case_name', async ({ page, skye, banorte }) => {

9. ASSERTIONS — always use expect() from @playwright/test; never console.log as a check.

10. OUTPUT — return ONLY the TypeScript file content.
    No markdown code fences, no explanations, no comments outside the code.

11. TYPE SAFETY — the file must pass `tsc --noEmit --skipLibCheck`.
"""

# ════════════════════════════════════════════════════════════════════════════════
# FEW-SHOT EXAMPLES  (shared across providers)
# ════════════════════════════════════════════════════════════════════════════════
FEW_SHOTS = [
    {
        "user": """Framework context: [truncated]

Test Case JSON:
{
  "test_script_num": "RB001",
  "module": "RB_Pets_Landing_Page",
  "test_case_name": "Verify Pet landing page with newly added 3 tabs from public page",
  "description": "Verify user sees 3 tabs: Coberturas base, Servicios opcionales, Exclusiones",
  "steps": [
    {"step_no": 1, "action": "Navigate to the application URL", "input_data": ""},
    {"step_no": 2, "action": "Click on Mascotas menu item", "input_data": ""},
    {"step_no": 3, "action": "Click Ver seguro button", "input_data": ""},
    {"step_no": 4, "action": "Verify 3 tabs are visible", "input_data": ""}
  ],
  "expected_results": "User should see 3 tabs"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../../fixtures/Fixtures';
import { expect } from '@playwright/test';
import MainPage from '../../pages/MainPage';
import PetsPage from '../../pages/PetsPage';

test('RB001 - Verify Pet landing page with newly added 3 tabs from public page',
  async ({ page, skye, banorte }) => {

    const mainPage = new MainPage(page);
    const petsPage = new PetsPage(page, skye);

    await test.step('Step 1: Navigate to the application URL', async () => {
      await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('img[alt="Mascotas"]', { state: 'visible', timeout: 30000 });
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
  "expected_results": "User is redirected to initiate a call"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../../fixtures/Fixtures';
import { expect } from '@playwright/test';
import MainPage from '../../pages/MainPage';

test('RB007 - Verify phone link is clickable', async ({ page, skye, banorte }) => {

    const mainPage = new MainPage(page);

    await test.step('Step 1: Navigate to the application URL', async () => {
      await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('img[alt="Mascotas"]', { state: 'visible', timeout: 30000 });
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
# MESSAGE BUILDERS
# ════════════════════════════════════════════════════════════════════════════════

def _main_user_content(test_case_json: dict, user_instruction: str, framework_context: str) -> str:
    return (
        f"Framework context (existing Page Objects, fixtures, custom commands):\n"
        f"```typescript\n{framework_context}\n```\n\n"
        f"Test Case JSON:\n"
        f"```json\n{json.dumps(test_case_json, indent=2)}\n```\n\n"
        f"User instruction: {user_instruction.strip() or 'Generate following the framework conventions strictly.'}\n\n"
        f"Generate the complete TypeScript test file now:"
    )


def _build_anthropic_messages(
    test_case_json: dict, user_instruction: str, framework_context: str
) -> list[dict]:
    messages: list[dict] = []
    for shot in FEW_SHOTS:
        messages.append({"role": "user",      "content": shot["user"]})
        messages.append({"role": "assistant", "content": shot["assistant"]})
    messages.append({"role": "user", "content": _main_user_content(
        test_case_json, user_instruction, framework_context
    )})
    return messages


def _build_gemini_history(
    test_case_json: dict, user_instruction: str, framework_context: str
) -> tuple[list[dict], str]:
    """Returns (history_list, last_user_message) in Gemini format."""
    # Gemini uses "model" instead of "assistant"
    history: list[dict] = []
    for shot in FEW_SHOTS:
        history.append({"role": "user",  "parts": [shot["user"]]})
        history.append({"role": "model", "parts": [shot["assistant"]]})
    last_msg = _main_user_content(test_case_json, user_instruction, framework_context)
    return history, last_msg


# ════════════════════════════════════════════════════════════════════════════════
# PROVIDER STREAMERS
# ════════════════════════════════════════════════════════════════════════════════

async def _stream_anthropic(
    messages: list[dict],
) -> AsyncGenerator[str, None]:
    async with _get_anthropic().messages.stream(
        model=settings.ANTHROPIC_MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text

        final = await stream.get_final_message()
        usage = final.usage
        logger.info(
            "Anthropic [%s] — in: %d tokens, out: %d tokens",
            settings.ANTHROPIC_MODEL, usage.input_tokens, usage.output_tokens,
        )
        stream_script.last_usage = {
            "provider": "anthropic",
            "model": settings.ANTHROPIC_MODEL,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }


async def _stream_gemini(
    history: list[dict], last_message: str
) -> AsyncGenerator[str, None]:
    _ensure_gemini()
    model = genai.GenerativeModel(
        model_name=settings.GEMINI_MODEL,
        system_instruction=SYSTEM_PROMPT,
    )
    chat = model.start_chat(history=history)
    response = await chat.send_message_async(last_message, stream=True)

    async for chunk in response:
        if chunk.text:
            yield chunk.text

    # Collect usage after stream exhausted
    try:
        meta = response.usage_metadata
        in_tok  = meta.prompt_token_count or 0
        out_tok = meta.candidates_token_count or 0
    except Exception:
        in_tok = out_tok = 0

    logger.info(
        "Gemini [%s] — in: %d tokens, out: %d tokens",
        settings.GEMINI_MODEL, in_tok, out_tok,
    )
    stream_script.last_usage = {
        "provider": "gemini",
        "model": settings.GEMINI_MODEL,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }


# ════════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════════════════════

async def stream_script(
    test_case_json: dict,
    user_instruction: str,
    framework_context: str,
    provider: LLMProvider | None = None,
) -> AsyncGenerator[str, None]:
    """
    Async generator — yields text chunks from the chosen LLM.

    `provider` overrides settings.LLM_PROVIDER for this call only.
    Falls back to settings.LLM_PROVIDER (set in .env) when None.
    """
    active: LLMProvider = provider or settings.LLM_PROVIDER  # type: ignore[assignment]
    logger.info("stream_script — provider=%s", active)

    if active == "gemini":
        history, last = _build_gemini_history(test_case_json, user_instruction, framework_context)
        async for chunk in _stream_gemini(history, last):
            yield chunk
    else:
        # Default: Anthropic
        messages = _build_anthropic_messages(test_case_json, user_instruction, framework_context)
        async for chunk in _stream_anthropic(messages):
            yield chunk


async def generate_script(
    test_case_json: dict,
    user_instruction: str = "",
    force_framework_refresh: bool = False,
    provider: LLMProvider | None = None,
) -> tuple[str, str, dict]:
    """Non-streaming wrapper. Returns (full_script, ctx_hash, usage_dict)."""
    ctx, ctx_hash = get_framework_context(force_refresh=force_framework_refresh)
    full_script = ""
    async for chunk in stream_script(test_case_json, user_instruction, ctx, provider):
        full_script += chunk
    usage = getattr(stream_script, "last_usage", {})
    return full_script, ctx_hash, usage


def active_provider_info() -> dict:
    """Returns info about available providers and current default."""
    return {
        "default_provider": settings.LLM_PROVIDER,
        "anthropic": {
            "model": settings.ANTHROPIC_MODEL,
            "configured": bool(settings.ANTHROPIC_API_KEY),
        },
        "gemini": {
            "model": settings.GEMINI_MODEL,
            "configured": bool(settings.GEMINI_API_KEY),
        },
    }
