"""Detect and mask card and identity data in text, with checksum validation so ordinary numbers are left alone.

Analogy: a mail-room clerk who blacks out card numbers before a letter is filed. A 16-digit string is only treated as
a card if it passes the Luhn check, and a 12-digit string only as an Aadhaar number if it passes the Verhoeff check.

Honest limits: these are validated heuristics, not a data-loss-prevention product. A random 16-digit number passes
Luhn about one time in ten, so an occasional order number is masked needlessly (the safe direction), and a card
number split by unusual formatting can be missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VD = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VP = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    if not digits.isdigit() or len(digits) != 12 or digits[0] in "01":
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


_CARD = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
_AADHAAR = re.compile(r"(?<![\w])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\w])")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_VPA = re.compile(r"(?<![\w.+-])[\w.-]{2,}@[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<![\w])(?:\+91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?!\d)")
_IFSC = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])")

_LABEL = {
    "card": "[CARD {last4}]",
    "aadhaar": "[AADHAAR]",
    "pan": "[PAN]",
    "email": "[EMAIL]",
    "upi_id": "[UPI_ID]",
    "phone": "[PHONE]",
    "ifsc": "[IFSC]",
}


def find_pii(text: str) -> list[Finding]:
    """Every validated sensitive span in `text`, without overlaps (the more specific detector wins)."""
    found: list[Finding] = []

    def free(s: int, e: int) -> bool:
        return all(e <= f.start or s >= f.end for f in found)

    for m in _CARD.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if luhn_ok(digits) and free(m.start(), m.end()):
            found.append(Finding("card", m.start(), m.end()))
    for m in _AADHAAR.finditer(text):
        if verhoeff_ok(re.sub(r"[ -]", "", m.group())) and free(m.start(), m.end()):
            found.append(Finding("aadhaar", m.start(), m.end()))
    for kind, pat in (("pan", _PAN), ("email", _EMAIL), ("upi_id", _VPA), ("ifsc", _IFSC), ("phone", _PHONE)):
        for m in pat.finditer(text):
            if free(m.start(), m.end()):
                found.append(Finding(kind, m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def redact(text: str, *, keep_card_last4: bool = True) -> tuple[str, list[Finding]]:
    """Mask every finding with a typed placeholder. A card keeps only its last four digits (the usual display rule)."""
    findings = find_pii(text)
    out, pos = [], 0
    for f in findings:
        out.append(text[pos : f.start])
        span = text[f.start : f.end]
        last4 = "****" + re.sub(r"\D", "", span)[-4:] if keep_card_last4 and f.kind == "card" else "****"
        out.append(_LABEL[f.kind].format(last4=last4))
        pos = f.end
    out.append(text[pos:])
    return "".join(out), findings
