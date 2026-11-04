"""A tiny PDF writer for tests, so we can build real multi-page PDFs offline with known text on known pages.

Not a general PDF library: just Helvetica text on Letter pages, enough for pypdf to extract it back.
"""

from __future__ import annotations

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]] | list[str]) -> bytes:
    """`pages` is a list of pages; each page is a list of text lines (or one string)."""
    norm = [[p] if isinstance(p, str) else list(p) for p in pages]
    objs: list[bytes] = []
    n = len(norm)
    # object ids: 1 catalog, 2 pages, 3 font, then (page, content) pairs starting at 4
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, lines in enumerate(norm):
        ops = ["BT", "/F1 11 Tf", "50 740 Td", "14 TL"]
        ops += [f"({_esc(ln)}) Tj T*" for ln in lines]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def write_pdf(path: Path, pages: list[list[str]] | list[str]) -> Path:
    Path(path).write_bytes(make_pdf(pages))
    return Path(path)


def long_lines(prefix: str, n: int = 30) -> list[str]:
    """n lines of filler prose, each unique, so a page has enough text to look 'real' (not scanned)."""
    return [f"{prefix} sentence number {i} talks about subject {prefix}{i} in some detail." for i in range(n)]
