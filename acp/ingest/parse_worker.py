"""The sealed side room: parse one PDF in a separate process with a memory cap and a time limit.

Run as `python -m acp.ingest.parse_worker <pdf> <out.jsonl> <max_pages>`. Why a subprocess: PDF parsers read
untrusted, complicated files. If a malicious or broken PDF makes the parser crash, hang or eat all memory,
only this child dies; the web server keeps running. `parse_pdf` is the parent-side helper.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from acp.ingest.pdf import PageText, ParsedPdf, PdfError, extract_pdf

MEMORY_LIMIT_BYTES = 2 * 1024**3


def _limit_memory() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):
        pass  # not enforceable on every OS (e.g. macOS); the timeout still applies


def _main(pdf: str, out: str, max_pages: int) -> int:
    _limit_memory()
    try:
        parsed = extract_pdf(Path(pdf), max_pages)
    except PdfError as e:
        Path(out).write_text(json.dumps({"error": {"code": e.code, "message": str(e)}}) + "\n")
        return 2
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"sections": parsed.sections, "warnings": parsed.warnings}) + "\n")
        for p in parsed.pages:
            f.write(json.dumps({"page": p.page, "label": p.label, "text": p.text}) + "\n")
    return 0


def parse_pdf(path: Path, out_dir: Path, max_pages: int, timeout_s: int) -> ParsedPdf:
    """Parse `path` in a child process. Raises PdfError (with a code) on any refusal, crash or timeout."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (Path(path).stem + ".pages.jsonl")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "acp.ingest.parse_worker", str(path), str(out), str(max_pages)],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise PdfError("timeout", f"Reading this PDF took longer than {timeout_s} seconds, so it was stopped.") from e
    if not out.is_file():
        raise PdfError("corrupt", "This PDF could not be processed (the reader crashed or ran out of memory).")
    lines = out.read_text(encoding="utf-8").splitlines()
    try:
        first = json.loads(lines[0])
    except (IndexError, json.JSONDecodeError) as e:
        raise PdfError("corrupt", "This PDF could not be processed.") from e
    if "error" in first:
        raise PdfError(first["error"]["code"], first["error"]["message"])
    if proc.returncode != 0:
        raise PdfError("corrupt", "This PDF could not be processed.")
    pages = [PageText(**json.loads(line)) for line in lines[1:]]
    return ParsedPdf(pages=pages, sections=[(p, t) for p, t in first["sections"]], warnings=first["warnings"])


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
