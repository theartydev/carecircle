"""
app/followup.py
---------------
Deterministic follow-up date derivation for CareCircle.

Converts a documented follow-up instruction into a structured follow-up
state using only Python date arithmetic — NO LLM involvement.

Rules:
- Supported: "after N days", "after N weeks", explicit ISO/named dates.
- Unsupported/ambiguous: "next month", "soon", "as needed", "later", etc.
- If the document_date is missing or unparseable, we cannot calculate.
- We never guess. Ambiguous = NEEDS_CLARIFICATION.

All data is synthetic demo data.
"""

import re
from datetime import date, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Public status constants
# ---------------------------------------------------------------------------
STATUS_AWAITING   = "AWAITING_CONFIRMATION"
STATUS_CONFIRMED  = "CONFIRMED"
STATUS_NEEDS_CLAR = "NEEDS_CLARIFICATION"
STATUS_IGNORED    = "IGNORED"

# ---------------------------------------------------------------------------
# Month name → number (English, case-insensitive)
# ---------------------------------------------------------------------------
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_document_date(document_date_str: Optional[str]) -> Optional[date]:
    """
    Try to parse a document_date string into a date object.
    Handles ISO (2026-08-26), "26 August 2026", "August 26 2026".
    Returns None on failure.
    """
    if not document_date_str:
        return None
    s = document_date_str.strip()

    # ISO: YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    # "DD Month YYYY" or "Month DD YYYY" or "DD Month, YYYY"
    m = re.search(
        r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})|([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})",
        s,
    )
    if m:
        if m.group(1):  # DD Month YYYY
            day, month_name, year = int(m[1]), m[2].lower(), int(m[3])
        else:           # Month DD YYYY
            month_name, day, year = m[4].lower(), int(m[5]), int(m[6])
        month = _MONTH_NAMES.get(month_name)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Pattern matching for duration instructions
# ---------------------------------------------------------------------------

_DURATION_PATTERNS = [
    # "after 4 weeks" / "in 4 weeks" / "4 weeks from now"
    (re.compile(r"\b(\d+)\s+week(?:s)?\b", re.IGNORECASE), "weeks"),
    # "after 30 days" / "in 30 days"
    (re.compile(r"\b(\d+)\s+day(?:s)?\b",  re.IGNORECASE), "days"),
    # "after 2 months" — treated as 30*N days (deterministic approximation)
    (re.compile(r"\b(\d+)\s+month(?:s)?\b", re.IGNORECASE), "months"),
]

# Patterns that indicate ambiguity — we cannot derive a deterministic date
_AMBIGUOUS_PATTERNS = [
    re.compile(r"\bnext\s+(?:month|week|visit|appointment)\b", re.IGNORECASE),
    re.compile(r"\bsoon\b",      re.IGNORECASE),
    re.compile(r"\bas\s+needed\b", re.IGNORECASE),
    re.compile(r"\blater\b",     re.IGNORECASE),
    re.compile(r"\bif\s+(?:needed|required|necessary)\b", re.IGNORECASE),
    re.compile(r"\bwhen\s+(?:required|needed)\b", re.IGNORECASE),
    re.compile(r"\broutine\b",   re.IGNORECASE),
    re.compile(r"\bprn\b",       re.IGNORECASE),
]

_EXPLICIT_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b"
    r"|\b(\d{4})-(\d{2})-(\d{2})\b"
)


def derive_followup(
    instruction: Optional[str],
    document_date_str: Optional[str],
) -> dict:
    """
    Derive a structured follow-up state from a documented instruction.

    Returns a dict with keys:
      source_instruction, suggested_date, confirmed_date,
      status, requires_human_confirmation

    Never invents dates for ambiguous instructions.
    """
    base = {
        "source_instruction":         instruction,
        "suggested_date":             None,
        "confirmed_date":             None,
        "status":                     STATUS_NEEDS_CLAR,
        "requires_human_confirmation": True,
    }

    if not instruction or not instruction.strip():
        base["source_instruction"] = None
        return base

    instr = instruction.strip()

    # 1. Check for explicit date in the instruction itself
    m = _EXPLICIT_DATE_PATTERN.search(instr)
    if m:
        if m.group(1):  # DD Month YYYY
            day, month_name, year = int(m[1]), m[2].lower(), int(m[3])
            month = _MONTH_NAMES.get(month_name)
            if month:
                try:
                    d = date(year, month, day)
                    base["suggested_date"] = d.isoformat()
                    base["status"] = STATUS_AWAITING
                    return base
                except ValueError:
                    pass
        elif m.group(4):  # YYYY-MM-DD
            try:
                d = date(int(m[4]), int(m[5]), int(m[6]))
                base["suggested_date"] = d.isoformat()
                base["status"] = STATUS_AWAITING
                return base
            except ValueError:
                pass

    # 2. Check ambiguous patterns — must come BEFORE duration check
    #    so "review next month" doesn't partially match "month" duration
    for pattern in _AMBIGUOUS_PATTERNS:
        if pattern.search(instr):
            base["status"] = STATUS_NEEDS_CLAR
            return base

    # 3. Duration patterns (require document_date)
    doc_date = _parse_document_date(document_date_str)

    for pattern, unit in _DURATION_PATTERNS:
        m = pattern.search(instr)
        if m:
            n = int(m.group(1))
            if doc_date is None:
                # Cannot calculate without a base date
                base["status"] = STATUS_NEEDS_CLAR
                return base
            if unit == "days":
                suggested = doc_date + timedelta(days=n)
            elif unit == "weeks":
                suggested = doc_date + timedelta(weeks=n)
            else:  # months — 30*n days approximation
                suggested = doc_date + timedelta(days=30 * n)
            base["suggested_date"] = suggested.isoformat()
            base["status"] = STATUS_AWAITING
            return base

    # 4. Nothing matched — needs clarification
    return base


def confirm_followup(current: dict, confirmed_date_iso: str) -> dict:
    """Return an updated follow-up dict with status CONFIRMED."""
    updated = dict(current)
    updated["confirmed_date"] = confirmed_date_iso
    updated["status"] = STATUS_CONFIRMED
    updated["requires_human_confirmation"] = False
    return updated


def ignore_followup(current: dict) -> dict:
    """Return an updated follow-up dict with status IGNORED."""
    updated = dict(current)
    updated["status"] = STATUS_IGNORED
    updated["requires_human_confirmation"] = False
    return updated
