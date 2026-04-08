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
follows the AI_Automation_MGA (skye-e2e-tests) framework conventions.

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
     import MainPage   from '../../pages/MainPage';       ← DEFAULT export — NO braces
     import LoginPage  from '../../pages/LoginPage';      ← DEFAULT export — NO braces (only if used)

   NEVER write:  import { MainPage } from '../../pages/MainPage';   ← WRONG
   NEVER write:  import { LoginPage } from '../../pages/LoginPage'; ← WRONG
   NEVER write:  import { test } from '@playwright/test';           ← WRONG (use Fixtures)
   NEVER write:  '../fixtures/...' or '../pages/...'                ← WRONG depth
   NEVER import 'allure-js-commons' or any allure package.
   NEVER import SkyeAttributeCommands or MGACommands — they come from the fixture.

   RULE: Only import a page class if you actually instantiate it with `new`.
     import MainPage  from '../../pages/MainPage';   ← only if you write  new MainPage(page)
     import LoginPage from '../../pages/LoginPage';  ← only if you write  new LoginPage(page)

2. FIXTURE DESTRUCTURING — always exactly:
     async ({ page, skye, mga }) => {
   • `page` → standard Playwright Page object
   • `skye` → SkyeAttributeCommands instance  (do NOT import this class)
   • `mga`  → MGACommands instance             (do NOT import this class)

3. PAGE OBJECT CONSTRUCTORS — use the correct signatures:
   • new MainPage(page)   ← ONE arg: Page only
   • new LoginPage(page)  ← ONE arg: Page only

4. AVAILABLE METHODS — only call methods that are listed here. Never invent method names.

   MainPage methods:
     mainPage.goto()                        → navigate to /page/internal/en/US/index
     mainPage.selectProductByAlt(altText)   → click product image by alt text (string)
     mainPage.isLoaded()                    → Promise<boolean> — check #sk-app is visible

   LoginPage methods:
     loginPage.goto()                       → navigate to login URL
     loginPage.login(email, password)       → fill username + password and click Log in
     loginPage.isLoggedIn()                 → Promise<boolean>

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
     skye.selectFromFlexDataSearch(testId, inputValue, expectedValue)
     skye.selectFromFlexDataDropdown(skTestId, inputValue, expectedValueSavedToDb)
     skye.uploadToResource(skTestId, filePaths)
     skye.verifyNoLoadingIcon(skTestId)
     skye.fillRichTextArea(testId, inputValue, validateText)

   mga methods (MGACommands — use via `mga`, no import needed):
     mga.handlePopupAceptar(skTestId, processName, buttonTitle, isThereACheckbox?, checkboxTestId?)
     mga.selectFromRangeDates(testId, dateFrom, dateTo)  ← dates: 'dd.MM.yyyy'

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
   a) Use `waitUntil: 'domcontentloaded'` — NEVER 'networkidle' (hangs on SPAs).
   b) Immediately after goto(), add page.waitForSelector() for the FIRST element
      the test will interact with. The explicit timeout: 30000 is required.
   c) The selector in waitForSelector MUST match what Step 2 will click/verify.

   Example — if Step 2 checks the dashboard heading:
     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('#sk-app', { state: 'visible', timeout: 30000 });

   Example — if Step 2 clicks a nav menu item:
     await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
     await page.waitForSelector('[data-testid="sk-nav-menu"]', { state: 'visible', timeout: 30000 });

7. NO ALLURE — do NOT add allure.tag(), allure.story(), or similar calls.

8. TEST NAMING — follow this exact pattern:
     test('TC001 - Short title matching the test_case_name', async ({ page, skye, mga }) => {

9. ASSERTIONS — always use expect() from @playwright/test; never console.log as a check.

10. OUTPUT FORMAT:
    a) SINGLE FILE (default): Return ONLY the .spec.ts content. No markdown fences.
    b) PAGE CLASS MODE: When the user EXPLICITLY asks to create a NEW page object class,
       output TWO sections with these EXACT markers:

       // === PAGE_CLASS: ClassName.ts ===
       <page class TypeScript code here>
       // === SPEC_FILE ===
       <spec file TypeScript code here>

       Page class rules: export default class, accept Page in constructor,
       import { Page, Locator } from '@playwright/test', extend BasePage if it exists.
       Spec file rules: import the class via default import from '../../pages/ClassName'.

    If the user does NOT ask for a page class, use mode (a) — single file only.
    NEVER use markdown fences (```). NEVER output "File 1:" or "File 2:" headers.

11. TYPE SAFETY — the file must pass `tsc --noEmit --skipLibCheck`.

12. DOM CONTEXT — when a "LIVE PAGE DOM CONTEXT" section is provided in the user message,
    you MUST prefer the real selectors listed there over invented selectors.
    Use data-testid → page.getByTestId('...'), id → page.locator('#...'),
    aria-label → page.getByLabel('...'), role → page.getByRole('...').
    Never invent selectors when real DOM selectors are provided.
    If the DOM shows selector='[data-testid="login-btn"]', use page.getByTestId('login-btn').
"""

# ════════════════════════════════════════════════════════════════════════════════
# FEW-SHOT EXAMPLES  (shared across providers)
# ════════════════════════════════════════════════════════════════════════════════
FEW_SHOTS = [
    {
        "user": """Framework context: [truncated]

Test Case JSON:
{
  "test_script_num": "DASH_001",
  "module": "Dashboard",
  "test_case_name": "Validate Dashboard Header Loads",
  "description": "Verify the MGA dashboard loads and the header is visible after login",
  "steps": [
    {"step_no": 1, "action": "Navigate to the application URL", "input_data": ""},
    {"step_no": 2, "action": "Verify the dashboard main container is visible", "input_data": ""},
    {"step_no": 3, "action": "Verify page title or heading is displayed", "input_data": ""}
  ],
  "expected_results": "Dashboard header and main container are visible"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../../fixtures/Fixtures';
import { expect } from '@playwright/test';
import MainPage from '../../pages/MainPage';

test('DASH_001 - Validate Dashboard Header Loads', async ({ page, skye, mga }) => {

    const mainPage = new MainPage(page);

    await test.step('Step 1: Navigate to the application URL', async () => {
        await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('#sk-app', { state: 'visible', timeout: 30000 });
    });

    await test.step('Step 2: Verify the dashboard main container is visible', async () => {
        await expect(page.locator('#sk-app')).toBeVisible();
        const isLoaded = await mainPage.isLoaded();
        expect(isLoaded).toBe(true);
    });

    await test.step('Step 3: Verify page title or heading is displayed', async () => {
        await expect(page.locator('h1, h2, [data-testid="page-title"]').first()).toBeVisible();
    });
});""",
    },
    {
        "user": """Framework context: [truncated]

Test Case JSON:
{
  "test_script_num": "MGA_002",
  "module": "Login",
  "test_case_name": "Validate Login with Valid Credentials",
  "description": "Verify user can log in with valid username and password",
  "steps": [
    {"step_no": 1, "action": "Navigate to the login page", "input_data": ""},
    {"step_no": 2, "action": "Enter username", "input_data": "usercc"},
    {"step_no": 3, "action": "Enter password", "input_data": "MGA@1234"},
    {"step_no": 4, "action": "Click Log in button", "input_data": ""},
    {"step_no": 5, "action": "Verify user is redirected to the dashboard", "input_data": ""}
  ],
  "expected_results": "User successfully logs in and sees the dashboard"
}

User instruction: Generate a Playwright/TypeScript test following the framework conventions.""",

        "assistant": """import { test } from '../../fixtures/Fixtures';
import { expect } from '@playwright/test';
import LoginPage from '../../pages/LoginPage';

test('MGA_002 - Validate Login with Valid Credentials', async ({ page, skye, mga }) => {

    const loginPage = new LoginPage(page);

    await test.step('Step 1: Navigate to the login page', async () => {
        await page.goto(process.env.pw_HOST!, { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('[placeholder="Enter username"]', { state: 'visible', timeout: 30000 });
    });

    await test.step('Step 2 & 3 & 4: Login with valid credentials', async () => {
        await loginPage.login(process.env.pw_TESTUSER!, process.env.pw_PASSWORD!);
    });

    await test.step('Step 5: Verify user is redirected to the dashboard', async () => {
        await page.waitForURL('**/page/internal/**', { timeout: 30000 });
        await expect(page.locator('#sk-app')).toBeVisible();
    });
});""",
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDERS
# ════════════════════════════════════════════════════════════════════════════════

def _main_user_content(
    test_case_json: dict, user_instruction: str, framework_context: str,
    dom_context: str = "",
) -> str:
    instr = user_instruction.strip()
    extra_block = ""
    if instr:
        extra_block = (
            f"\n\n"
            f"═══════════════════════════════════════════════════════════\n"
            f"EXTRA INSTRUCTIONS (HIGHEST PRIORITY — you MUST follow these):\n"
            f"═══════════════════════════════════════════════════════════\n"
            f"{instr}\n"
            f"═══════════════════════════════════════════════════════════\n"
            f"\n"
            f"IMPORTANT: The above extra instructions OVERRIDE the default behavior.\n"
            f"You MUST incorporate every requirement listed above into the generated test.\n"
        )
    dom_block = ""
    if dom_context.strip():
        dom_block = (
            f"\n\n{dom_context}\n\n"
            f"IMPORTANT: Use the REAL selectors from the DOM context above.\n"
            f"Prefer data-testid selectors (page.getByTestId) when available.\n"
        )
    return (
        f"Framework context (existing Page Objects, fixtures, custom commands):\n"
        f"```typescript\n{framework_context}\n```\n\n"
        f"{dom_block}"
        f"Test Case JSON:\n"
        f"```json\n{json.dumps(test_case_json, indent=2)}\n```\n\n"
        f"{extra_block}"
        f"Generate the complete TypeScript test file now. "
        f"{'Follow the framework conventions strictly.' if not instr else 'Follow both the framework conventions AND the extra instructions above.'}"
    )


def _build_anthropic_messages(
    test_case_json: dict, user_instruction: str, framework_context: str,
    dom_context: str = "",
) -> list[dict]:
    messages: list[dict] = []
    for shot in FEW_SHOTS:
        messages.append({"role": "user",      "content": shot["user"]})
        messages.append({"role": "assistant", "content": shot["assistant"]})

    # If the user provided extra instructions, add a reminder message before the
    # main request so the LLM sees it prominently in the conversation.
    instr = user_instruction.strip()
    if instr:
        messages.append({"role": "user", "content": (
            f"IMPORTANT — Before you generate the next script, you MUST follow "
            f"these extra instructions provided by the user:\n\n{instr}\n\n"
            f"Acknowledge that you will follow these instructions."
        )})
        messages.append({"role": "assistant", "content": (
            f"Understood. I will follow these extra instructions:\n"
            f"- {instr}\n\n"
            f"I will incorporate them into the generated test script while "
            f"maintaining all framework conventions."
        )})

    messages.append({"role": "user", "content": _main_user_content(
        test_case_json, user_instruction, framework_context, dom_context
    )})
    return messages


def _build_gemini_history(
    test_case_json: dict, user_instruction: str, framework_context: str,
    dom_context: str = "",
) -> tuple[list[dict], str]:
    """Returns (history_list, last_user_message) in Gemini format."""
    # Gemini uses "model" instead of "assistant"
    history: list[dict] = []
    for shot in FEW_SHOTS:
        history.append({"role": "user",  "parts": [shot["user"]]})
        history.append({"role": "model", "parts": [shot["assistant"]]})

    # If the user provided extra instructions, add a reminder exchange
    instr = user_instruction.strip()
    if instr:
        history.append({"role": "user", "parts": [(
            f"IMPORTANT — Before you generate the next script, you MUST follow "
            f"these extra instructions provided by the user:\n\n{instr}\n\n"
            f"Acknowledge that you will follow these instructions."
        )]})
        history.append({"role": "model", "parts": [(
            f"Understood. I will follow these extra instructions:\n"
            f"- {instr}\n\n"
            f"I will incorporate them into the generated test script while "
            f"maintaining all framework conventions."
        )]})

    last_msg = _main_user_content(test_case_json, user_instruction, framework_context, dom_context)
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
    dom_context: str = "",
) -> AsyncGenerator[str, None]:
    """
    Async generator — yields text chunks from the chosen LLM.

    `provider` overrides settings.LLM_PROVIDER for this call only.
    Falls back to settings.LLM_PROVIDER (set in .env) when None.
    `dom_context` — optional live DOM context from page crawling.
    """
    active: LLMProvider = provider or settings.LLM_PROVIDER  # type: ignore[assignment]
    logger.info("stream_script — provider=%s, dom_context_len=%d", active, len(dom_context))

    if active == "gemini":
        history, last = _build_gemini_history(test_case_json, user_instruction, framework_context, dom_context)
        async for chunk in _stream_gemini(history, last):
            yield chunk
    else:
        # Default: Anthropic
        messages = _build_anthropic_messages(test_case_json, user_instruction, framework_context, dom_context)
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


# ════════════════════════════════════════════════════════════════════════════════
# FIX MODE — Self-Healing (Auto-Fix Failed Scripts)
# ════════════════════════════════════════════════════════════════════════════════

FIX_SYSTEM_PROMPT = SYSTEM_PROMPT + """

=== FIX MODE ===
You are now in FIX MODE. You are given a failing Playwright/TypeScript test
and the Playwright error output from its execution.

Your task:
1. Analyze the error carefully (wrong selector? timeout? assertion mismatch?)
2. Fix ONLY what is broken — preserve the test structure, steps, and logic
3. If a selector is wrong, use a more robust alternative (prefer data-testid, getByRole, getByLabel)
4. If a timeout occurred, add appropriate waitForSelector() calls
5. If an assertion failed, check the expected value against what the error says
6. Return ONLY the corrected TypeScript .spec.ts code — no explanations

CRITICAL: The output must be a SINGLE valid TypeScript file starting with `import`.
No markdown fences, no explanations before or after the code.
"""


async def stream_fix_script(
    original_code: str,
    error_message: str,
    framework_context: str,
    provider: LLMProvider | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream a fixed version of a failing test script.
    Takes the original code + Playwright error output → yields corrected code chunks.
    """
    active: LLMProvider = provider or settings.LLM_PROVIDER  # type: ignore[assignment]
    logger.info("stream_fix_script — provider=%s, error_len=%d", active, len(error_message))

    user_message = (
        f"Here is the ORIGINAL failing Playwright test:\n\n"
        f"```typescript\n{original_code}\n```\n\n"
        f"Framework context (existing Page Objects, fixtures, custom commands):\n"
        f"```typescript\n{framework_context}\n```\n\n"
        f"Here is the Playwright ERROR output from running this test:\n\n"
        f"```\n{error_message}\n```\n\n"
        f"Fix the test so it passes. Return ONLY the corrected TypeScript code."
    )

    if active == "gemini":
        _ensure_gemini()
        model = genai.GenerativeModel(
            model_name=settings.GEMINI_MODEL,
            system_instruction=FIX_SYSTEM_PROMPT,
        )
        chat = model.start_chat(history=[])
        response = await chat.send_message_async(user_message, stream=True)
        async for chunk in response:
            if chunk.text:
                yield chunk.text
        try:
            meta = response.usage_metadata
            stream_fix_script.last_usage = {
                "provider": "gemini", "model": settings.GEMINI_MODEL,
                "input_tokens": meta.prompt_token_count or 0,
                "output_tokens": meta.candidates_token_count or 0,
            }
        except Exception:
            stream_fix_script.last_usage = {}
    else:
        messages = [{"role": "user", "content": user_message}]
        async with _get_anthropic().messages.stream(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=8000,
            system=FIX_SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
            usage = final.usage
            stream_fix_script.last_usage = {
                "provider": "anthropic", "model": settings.ANTHROPIC_MODEL,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }
