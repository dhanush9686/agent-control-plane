"""Numeric fidelity: every amount in an answer must trace back to a tool result (or to the user's own words).

Analogy: a cashier who may only read out figures printed on the receipt. If the model says "₹1,499.00" and no tool
returned 149900 paise, the figure is flagged. The model is also kept away from arithmetic: totals must come from a
tool, because a sum the model computed cannot be checked against a source.

Only currency-marked amounts (₹, Rs, INR) and two-decimal figures are checked; ids, dates and counts are ignored.
That is a deliberate narrow scope, not full coverage of every number a sentence can contain.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

_ID = re.compile(r"\b(?:pay|rfnd|setl|utr|ref|draft)_[A-Za-z0-9]+\b", re.I)
_MARKED = re.compile(r"(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d+)?)", re.I)
_WORD_UNIT = re.compile(r"\b([\d,]+(?:\.\d+)?)\s*(?:rupees|inr)\b", re.I)
_DECIMAL2 = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})*\.\d{2}|\d+\.\d{2})(?![\w.])")


def _dec(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def amounts_in(text: str) -> list[Decimal]:
    """Rupee amounts mentioned in `text`, as exact decimals."""
    text = _ID.sub(" ", text)
    out: list[Decimal] = []
    for pat in (_MARKED, _WORD_UNIT, _DECIMAL2):
        for m in pat.finditer(text):
            d = _dec(m.group(1))
            if d is not None and d not in out:
                out.append(d)
    return out


def paise_values(obj: Any) -> set[Decimal]:
    """All integer-paise fields in a tool result (any key ending `_paise`), converted to rupees."""
    found: set[Decimal] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_paise") and isinstance(v, int) and not isinstance(v, bool):
                found.add((Decimal(v) / 100).quantize(Decimal("0.01")))
            else:
                found |= paise_values(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= paise_values(v)
    return found


def unsupported_amounts(answer: str, tool_results: list[Any], user_text: str = "") -> list[str]:
    """Amounts stated in `answer` that appear in no tool result and not in the user's message (as '1499.00')."""
    allowed: set[Decimal] = set(amounts_in(user_text))
    for r in tool_results:
        allowed |= paise_values(r if not isinstance(r, str) else _loads(r))
    return [f"{a:.2f}" for a in amounts_in(answer) if a not in allowed]


def _loads(s: str) -> Any:
    try:
        return json.loads(s)
    except ValueError:
        return {}
