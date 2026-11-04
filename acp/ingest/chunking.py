"""Cut page text into chunks that remember their page and section.

Analogy: index cards. Each card holds a few sentences (~900 characters), says which page it came from and which
chapter, and overlaps the previous card a little so an idea cut in half is still whole on one of them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from acp.ingest.pdf import PageText, ParsedPdf, PdfError

_HEADING = re.compile(
    r"^(?:(?:chapter|part|section)\s+[\divxlc]+\b.*|\d+(?:\.\d+)*\.?\s+[A-Z].*|[A-Z][A-Z0-9 ,:;'&-]{3,})$"
)
_DIGITS = re.compile(r"\d+")


@dataclass
class Chunk:
    idx: int  # position in the document (also its row in the vector index)
    page: int
    page_label: str | None
    section: str | None
    text: str


def normalize(text: str) -> str:
    """Repair PDF line wrapping: re-join hyphenated words, join wrapped lines, keep paragraph breaks."""
    text = text.replace("­", "").replace("\x00", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # exam-\nple -> example
    paras = [re.sub(r"[ \t]*\n[ \t]*", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(re.sub(r"[ \t]+", " ", p) for p in paras if p)


def _header_key(line: str) -> str:
    """Identity of a line for header/footer detection. Digits are masked ONLY when they sit at the very start
    or end ('Page 17', 'Annual Report | 17', '17 | Title'), so body sentences that merely contain a number
    ('Revenue in 2023 grew 12% ...') never match each other."""
    s = line.strip()
    if re.match(r"^[\W_]*\d+\b", s) or re.search(r"\b\d+[\W_]*$", s):
        return _DIGITS.sub("#", s)
    return s


def strip_repeated_lines(
    pages: list[PageText], threshold: float = 0.3, max_len: int = 90, edge: int = 3
) -> list[PageText]:
    """Remove running headers/footers and bare page numbers.

    Only the first/last `edge` lines of a page are candidates (that is where headers and footers live), they
    must be short, and they must repeat on more than `threshold` of the pages.
    """
    if len(pages) < 4:
        return pages

    def edges(lines: list[str]) -> set[int]:
        idx = [i for i, ln in enumerate(lines) if ln.strip()]
        return set(idx[:edge]) | set(idx[-edge:])

    counts: Counter[str] = Counter()
    split = [p.text.splitlines() for p in pages]
    for lines in split:
        counts.update({_header_key(lines[i]) for i in edges(lines) if len(lines[i].strip()) <= max_len})
    limit = threshold * len(pages)
    repeated = {k for k, v in counts.items() if v > limit}
    cleaned = []
    for p, lines in zip(pages, split, strict=True):
        drop = {
            i
            for i in edges(lines)
            if _header_key(lines[i]) in repeated or re.fullmatch(r"\s*[-–]?\s*\d{1,4}\s*[-–]?\s*", lines[i])
        }
        cleaned.append(PageText(p.page, p.label, "\n".join(ln for i, ln in enumerate(lines) if i not in drop)))
    return cleaned


def detect_heading(text: str) -> str | None:
    """Cheap heading guess for PDFs without an outline: a short first line that looks like a title."""
    for ln in text.splitlines()[:3]:
        ln = ln.strip()
        if 3 <= len(ln) <= 80 and _HEADING.match(ln):
            return ln
    return None


def _split(text: str, size: int, overlap: int) -> list[str]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap, separators=["\n\n", "\n", ". ", " ", ""]
    )
    pieces = [p.strip() for p in splitter.split_text(text) if p.strip()]
    if len(pieces) >= 2 and len(pieces[-1]) < size * 0.25:  # merge a tiny tail into the previous chunk
        pieces[-2] = f"{pieces[-2]} {pieces[-1]}"
        pieces.pop()
    return pieces


def chunk_document(parsed: ParsedPdf, size: int, overlap: int, max_chunks: int) -> list[Chunk]:
    """Page-aware chunks with section titles. Raises PdfError if the document would exceed `max_chunks`."""
    pages = strip_repeated_lines(parsed.pages)
    outline = sorted(parsed.sections)
    chunks: list[Chunk] = []
    current_section: str | None = None
    carry = ""  # tail of the previous page when it ended mid-sentence, so ideas spanning a page break survive
    for page in pages:
        body = normalize(page.text)
        if not body:
            continue
        outline_title = next((t for p, t in reversed(outline) if p <= page.page), None)
        current_section = outline_title or detect_heading(page.text) or current_section
        text = f"{carry} {body}".strip() if carry else body
        for piece in _split(text, size, overlap):
            chunks.append(Chunk(len(chunks), page.page, page.label, current_section, piece))
            if len(chunks) > max_chunks:
                raise PdfError("too_many_chunks", f"This document is too large to index (over {max_chunks} chunks).")
        carry = body[-overlap:] if overlap and not re.search(r"[.!?\"')\]]\s*$", body) else ""
    if not chunks:
        raise PdfError("scanned", "No searchable text was found in this PDF.")
    return chunks
