"""Read a PDF safely: validate, extract text per page, and find its outline (table of contents).

Analogy: the intake clerk. Before anything else they check the parcel really is a PDF, is not locked
(encrypted), is not absurdly long, and actually contains text (a scanned book is just pictures of pages).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from acp.errors import ACPError

MIN_CHARS_PER_PAGE = 50  # a page with less text than this is probably a picture
SCANNED_FRACTION = 0.5  # if more than half the pages are pictures, treat the file as scanned


class PdfError(ACPError):
    """A PDF we refuse or cannot read. `code` lets the API choose the HTTP status; the message is user-friendly."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PageText:
    page: int  # 1-based PDF page index (what a PDF viewer calls "page N")
    label: str | None  # the page number printed on the page (roman numerals, etc.), when the PDF defines one
    text: str


@dataclass
class ParsedPdf:
    pages: list[PageText]
    sections: list[tuple[int, str]] = field(default_factory=list)  # (start page, title) from the PDF outline
    warnings: list[str] = field(default_factory=list)


def looks_like_pdf(path: Path) -> bool:
    """Magic-bytes check: a real PDF starts with '%PDF-'. We never trust the filename or Content-Type."""
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-"


def _outline_sections(reader) -> list[tuple[int, str]]:  # noqa: ANN001 - pypdf type
    found: list[tuple[int, str]] = []

    def walk(items) -> None:  # noqa: ANN001
        for it in items:
            if isinstance(it, list):
                walk(it)
                continue
            try:
                found.append((reader.get_destination_page_number(it) + 1, str(it.title).strip()))
            except Exception:  # noqa: BLE001 - a broken bookmark must not fail the whole file
                continue

    try:
        walk(reader.outline)
    except Exception:  # noqa: BLE001
        return []
    return sorted({(p, t) for p, t in found if t}, key=lambda x: x[0])


def extract_pdf(path: Path, max_pages: int) -> ParsedPdf:
    """Open the PDF and return text for every page. Raises PdfError for files we will not process.

    Runs inside the sandboxed subprocess (see parse_worker.py); call `parse_pdf` from application code.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
    except (PdfReadError, ValueError, OSError) as e:
        raise PdfError("corrupt", "This file could not be read as a PDF (it may be damaged).") from e
    if reader.is_encrypted:
        raise PdfError("encrypted", "This PDF is password-protected. Remove the password and upload it again.")
    n = len(reader.pages)
    if n == 0:
        raise PdfError("corrupt", "This PDF has no pages.")
    if n > max_pages:
        raise PdfError("too_many_pages", f"This PDF has {n} pages; the limit is {max_pages}.")

    pages: list[PageText] = []
    warnings: list[str] = []
    try:
        labels = list(reader.page_labels)
    except Exception:  # noqa: BLE001
        labels = [None] * n
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001 - one bad page should not lose the other 499
            text = ""
            warnings.append(f"page {i + 1}: could not extract text ({type(e).__name__})")
        label = labels[i] if i < len(labels) and labels[i] and labels[i] != str(i + 1) else None
        pages.append(PageText(page=i + 1, label=label, text=text))

    thin = sum(1 for p in pages if len(p.text.strip()) < MIN_CHARS_PER_PAGE)
    if thin / n > SCANNED_FRACTION:
        raise PdfError(
            "scanned",
            "This PDF looks scanned (mostly images, very little text). OCR is not enabled, so it cannot be searched.",
        )
    return ParsedPdf(pages=pages, sections=_outline_sections(reader), warnings=warnings)
