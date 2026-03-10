"""
Excel Parser — maps Pet_LandingPage.xlsx columns to a normalized TestCase schema.

Exact column names from the file:
  - Test Script Num  → test_script_num   (e.g. RB001)
  - Module           → module            (e.g. RB_Pets_ Landing Page)
  - Test Case        → test_case_name    (human-readable title)
  - Description      → description       (what to verify)
  - Step             → steps             (multi-line steps string → list of dicts)
  - Expected Results → expected_results  (expected outcome)
"""
import re
import io
from pathlib import Path
from typing import Union
import openpyxl
from pydantic import BaseModel


# ── Pydantic schemas ─────────────────────────────────────────────────────────────

class TestStep(BaseModel):
    step_no: int
    action: str
    input_data: str = ""


class ParsedTestCase(BaseModel):
    test_script_num: str        # RB001
    module: str                  # RB_Pets_Landing_Page (cleaned)
    test_case_name: str          # full title
    description: str
    steps: list[TestStep]
    raw_steps: str               # original cell text (kept for audit)
    expected_results: str
    excel_source: str


# ── Column name normalizer ────────────────────────────────────────────────────────

# Maps possible header variations (after stripping + lower) to canonical keys
COLUMN_MAP = {
    "test script num": "test_script_num",
    "test script number": "test_script_num",
    "module": "module",
    "test case": "test_case_name",
    "test case name": "test_case_name",
    "description": "description",
    "step": "steps",
    "steps": "steps",
    "expected results": "expected_results",
    "expected result": "expected_results",
}


# ── Step parser ───────────────────────────────────────────────────────────────────

def _parse_steps(raw: str) -> list[TestStep]:
    """
    Handles multi-line step cells such as:
      1. Navigate to app
      2. Click Mascotas > Ver seguro
      3. Verify 3 tabs are visible

    Also handles plain newlines without numbering.
    """
    if not raw or not raw.strip():
        return [TestStep(step_no=1, action="See test case description")]

    # Split on numbered patterns like "1.", "2.", "a)", "b)" or newlines
    parts = re.split(r'(?:\r?\n)+|(?<!\d)(?:\d+[\.\)]\s+)', raw.strip())
    steps = []
    step_no = 1
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Try to detect inline input data (pattern: "enter <value>: <data>")
        input_match = re.search(
            r'(?:enter|input|type|fill)[:\s]+(.+)', part, re.IGNORECASE
        )
        input_data = input_match.group(1).strip() if input_match else ""

        steps.append(TestStep(step_no=step_no, action=part, input_data=input_data))
        step_no += 1

    return steps if steps else [TestStep(step_no=1, action=raw.strip())]


# ── Module name cleaner ───────────────────────────────────────────────────────────

def _clean_module(raw: str) -> str:
    """'RB_Pets_ Landing Page' → 'RB_Pets_Landing_Page'"""
    return re.sub(r'\s+', '_', raw.strip()).strip('_')


# ── Main parser ───────────────────────────────────────────────────────────────────

def parse_excel(source: Union[str, Path, bytes, io.BytesIO]) -> list[ParsedTestCase]:
    """
    Parse a .xlsx file and return a list of ParsedTestCase objects.

    Args:
        source: File path (str/Path) OR raw bytes OR BytesIO object.
    """
    if isinstance(source, bytes):
        source = io.BytesIO(source)

    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Excel file is empty")

    # ── Detect headers ────────────────────────────────────────────────────────────
    raw_headers = [str(h).strip() if h else "" for h in rows[0]]
    col_index: dict[str, int] = {}
    for idx, h in enumerate(raw_headers):
        canonical = COLUMN_MAP.get(h.lower())
        if canonical:
            col_index[canonical] = idx

    required = {"test_script_num", "test_case_name", "steps"}
    missing = required - set(col_index.keys())
    if missing:
        raise ValueError(
            f"Excel is missing required columns: {missing}. "
            f"Found headers: {raw_headers}"
        )

    # ── File name for audit ───────────────────────────────────────────────────────
    excel_src = str(source) if isinstance(source, (str, Path)) else "uploaded_file.xlsx"

    # ── Parse data rows ───────────────────────────────────────────────────────────
    test_cases: list[ParsedTestCase] = []
    for row in rows[1:]:
        def _cell(key: str, default: str = "") -> str:
            idx = col_index.get(key)
            if idx is None:
                return default
            val = row[idx]
            return str(val).strip() if val is not None else default

        test_script_num = _cell("test_script_num")
        if not test_script_num or test_script_num.lower() in ("none", ""):
            continue  # skip empty rows

        raw_steps_text = _cell("steps")
        steps = _parse_steps(raw_steps_text)

        tc = ParsedTestCase(
            test_script_num=test_script_num,
            module=_clean_module(_cell("module", "UnknownModule")),
            test_case_name=_cell("test_case_name"),
            description=_cell("description"),
            steps=steps,
            raw_steps=raw_steps_text,
            expected_results=_cell("expected_results"),
            excel_source=excel_src,
        )
        test_cases.append(tc)

    wb.close()
    return test_cases


# ── JSON serializer (for DB storage) ─────────────────────────────────────────────

def test_case_to_json(tc: ParsedTestCase) -> dict:
    """Convert ParsedTestCase to a plain dict for JSONB storage."""
    return {
        "test_script_num": tc.test_script_num,
        "module": tc.module,
        "test_case_name": tc.test_case_name,
        "description": tc.description,
        "steps": [s.model_dump() for s in tc.steps],
        "expected_results": tc.expected_results,
    }


# ── Standalone test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys, json

    path = sys.argv[1] if len(sys.argv) > 1 else "Pet_LandingPage.xlsx"
    cases = parse_excel(path)
    for tc in cases:
        print(json.dumps(test_case_to_json(tc), indent=2))
        print("-" * 60)
