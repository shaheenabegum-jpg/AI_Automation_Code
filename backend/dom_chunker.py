"""
DOM Chunker — transforms raw crawl data into concise LLM-friendly context.

Takes the output of dom_crawler.crawl_page() and produces a structured string
that can be injected into the LLM prompt alongside framework context.

Max output: 15,000 characters (~4K tokens).
"""
from __future__ import annotations

import re
from typing import Any

MAX_DOM_CONTEXT_CHARS = 15_000

# Common stop words to exclude from keyword matching
_STOP_WORDS = {
    "the", "and", "for", "that", "this", "with", "from", "are", "was",
    "will", "can", "has", "have", "been", "not", "but", "all", "any",
    "user", "page", "test", "verify", "should", "click", "enter", "see",
    "step", "expected", "result", "able",
}


def build_dom_context(
    crawl_result: dict[str, Any],
    test_case_json: dict | None = None,
    max_chars: int = MAX_DOM_CONTEXT_CHARS,
) -> str:
    """
    Build a concise DOM context string for the LLM.

    Args:
        crawl_result: Output from crawl_page()
        test_case_json: Optional test case dict for keyword relevance scoring
        max_chars: Maximum output length (default 15K)

    Returns:
        Formatted string ready to inject into LLM prompt, or "" on failure.
    """
    if not crawl_result or crawl_result.get("error"):
        return ""

    elements = crawl_result.get("elements", [])
    if not elements:
        return ""

    title = crawl_result.get("title", "")
    url = crawl_result.get("url", "")

    # Extract keywords from test case for relevance scoring
    keywords = _extract_keywords(test_case_json) if test_case_json else set()

    # Score and sort elements by relevance
    scored = [(_score_element(el, keywords), el) for el in elements]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Group elements by type
    groups: dict[str, list[dict]] = {
        "FORM INPUTS": [],
        "BUTTONS": [],
        "LINKS": [],
        "TABS / NAVIGATION": [],
        "OTHER INTERACTIVE": [],
    }

    caps = {
        "FORM INPUTS": 40,
        "BUTTONS": 30,
        "LINKS": 30,
        "TABS / NAVIGATION": 20,
        "OTHER INTERACTIVE": 20,
    }

    for _score, el in scored:
        tag = el.get("tag", "")
        role = el.get("role", "")
        group = _classify_element(tag, role)
        if len(groups[group]) < caps[group]:
            groups[group].append(el)

    # Build output
    lines = [
        "=== LIVE PAGE DOM CONTEXT (use these real selectors) ===",
        f"PAGE: {title} | {url}",
        f"TOTAL INTERACTIVE ELEMENTS: {crawl_result.get('element_count', len(elements))}",
        "",
    ]

    for group_name, group_elements in groups.items():
        if not group_elements:
            continue
        lines.append(f"{group_name}:")
        for el in group_elements:
            line = _format_element(el)
            if line:
                lines.append(f"  {line}")
        lines.append("")

    output = "\n".join(lines)

    # Truncate if needed
    if len(output) > max_chars:
        output = output[:max_chars - 20] + "\n... [truncated]"

    return output


def _extract_keywords(test_case_json: dict) -> set[str]:
    """Extract meaningful keywords from test case for relevance scoring."""
    text_parts = []
    for key in ("test_case_name", "description", "raw_steps", "expected_results"):
        val = test_case_json.get(key, "")
        if val:
            text_parts.append(str(val))

    # Also check parsed_json for step details
    parsed = test_case_json.get("parsed_json", {})
    if isinstance(parsed, dict):
        for step in parsed.get("steps", []):
            if isinstance(step, dict):
                text_parts.append(step.get("action", ""))
                text_parts.append(step.get("input_data", ""))
                text_parts.append(step.get("expected", ""))

    combined = " ".join(text_parts).lower()
    words = re.findall(r"[a-záéíóúñ]{3,}", combined)
    return {w for w in words if w not in _STOP_WORDS}


def _score_element(el: dict, keywords: set[str]) -> int:
    """Score an element by keyword relevance (higher = more relevant)."""
    if not keywords:
        return 0

    score = 0
    searchable = " ".join([
        el.get("text", ""),
        el.get("testId", ""),
        el.get("name", ""),
        el.get("ariaLabel", ""),
        el.get("placeholder", ""),
        el.get("id", ""),
    ]).lower()

    for kw in keywords:
        if kw in searchable:
            score += 2
            # Bonus for exact data-testid or name match
            if kw in (el.get("testId", "").lower() or el.get("name", "").lower()):
                score += 3

    # Bonus for having data-testid (framework-friendly selector)
    if el.get("testId"):
        score += 1

    return score


def _classify_element(tag: str, role: str) -> str:
    """Classify an element into a display group."""
    if tag in ("input", "textarea", "select"):
        return "FORM INPUTS"
    if tag == "button" or role == "button":
        return "BUTTONS"
    if tag == "a" or role == "link":
        return "LINKS"
    if role in ("tab", "menuitem", "navigation", "menu"):
        return "TABS / NAVIGATION"
    return "OTHER INTERACTIVE"


def _format_element(el: dict) -> str:
    """Format a single element as a concise one-liner."""
    parts = [el.get("tag", "unknown")]

    selector = el.get("selector", "")
    if selector:
        parts.append(f"selector='{selector}'")

    text = (el.get("text") or "").strip()
    if text:
        parts.append(f'text="{text[:50]}"')

    placeholder = (el.get("placeholder") or "")
    if placeholder:
        parts.append(f'placeholder="{placeholder[:40]}"')

    el_type = el.get("type", "")
    if el_type:
        parts.append(f"type={el_type}")

    href = el.get("href", "")
    if href and not href.startswith("javascript"):
        parts.append(f'href="{href[:60]}"')

    return " | ".join(parts)
