import pytest

from acp.ingest.chunking import chunk_document, detect_heading, normalize, strip_repeated_lines
from acp.ingest.parse_worker import parse_pdf
from acp.ingest.pdf import PageText, ParsedPdf, PdfError, extract_pdf, looks_like_pdf
from tests.pdfgen import long_lines, make_pdf, write_pdf


def _doc(tmp_path, pages, name="d.pdf"):
    return write_pdf(tmp_path / name, pages)


def test_magic_bytes_check(tmp_path):
    assert looks_like_pdf(_doc(tmp_path, [long_lines("a")]))
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"MZ\x90\x00 definitely an executable")
    assert not looks_like_pdf(fake)


def test_extract_gives_text_per_page_with_numbers(tmp_path):
    parsed = extract_pdf(_doc(tmp_path, [long_lines("alpha"), long_lines("beta"), long_lines("gamma")]), 100)
    assert [p.page for p in parsed.pages] == [1, 2, 3]
    assert "beta sentence number 3" in parsed.pages[1].text and "alpha" not in parsed.pages[1].text


def test_too_many_pages_is_refused(tmp_path):
    path = _doc(tmp_path, [long_lines(f"p{i}") for i in range(5)])
    with pytest.raises(PdfError) as e:
        extract_pdf(path, 3)
    assert e.value.code == "too_many_pages" and "5 pages" in str(e.value)


def test_garbage_file_is_reported_as_corrupt(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a real pdf body at all")
    with pytest.raises(PdfError) as e:
        extract_pdf(bad, 100)
    assert e.value.code == "corrupt"


def test_encrypted_pdf_is_refused_with_a_clear_message(tmp_path):
    from pypdf import PdfWriter

    src = _doc(tmp_path, [long_lines("a")])
    w = PdfWriter(clone_from=str(src))
    w.encrypt("secret", algorithm="RC4-128")
    enc = tmp_path / "enc.pdf"
    with open(enc, "wb") as f:
        w.write(f)
    with pytest.raises(PdfError) as e:
        extract_pdf(enc, 100)
    assert e.value.code == "encrypted" and "password" in str(e.value)


def test_scanned_pdf_with_almost_no_text_is_refused(tmp_path):
    path = _doc(tmp_path, [["x"], ["y"], ["z"], long_lines("only one real page")])
    with pytest.raises(PdfError) as e:
        extract_pdf(path, 100)
    assert e.value.code == "scanned"


def test_outline_sections_are_read(tmp_path):
    from pypdf import PdfWriter

    src = _doc(tmp_path, [long_lines(f"p{i}") for i in range(4)])
    w = PdfWriter(clone_from=str(src))
    w.add_outline_item("Chapter One", 0)
    w.add_outline_item("Chapter Two", 2)
    out = tmp_path / "outlined.pdf"
    with open(out, "wb") as f:
        w.write(f)
    assert extract_pdf(out, 100).sections == [(1, "Chapter One"), (3, "Chapter Two")]


def test_subprocess_parser_matches_in_process_and_reports_errors(tmp_path):
    ok = _doc(tmp_path, [long_lines("alpha"), long_lines("beta")], "ok.pdf")
    parsed = parse_pdf(ok, tmp_path / "work", max_pages=10, timeout_s=60)
    assert [p.page for p in parsed.pages] == [1, 2] and "beta" in parsed.pages[1].text
    with pytest.raises(PdfError) as e:  # a refusal inside the child comes back with its code
        parse_pdf(ok, tmp_path / "work", max_pages=1, timeout_s=60)
    assert e.value.code == "too_many_pages"


def test_subprocess_parser_timeout_is_a_clean_error(tmp_path):
    ok = _doc(tmp_path, [long_lines("alpha")], "t.pdf")
    with pytest.raises(PdfError) as e:
        parse_pdf(ok, tmp_path / "work", max_pages=10, timeout_s=0)
    assert e.value.code == "timeout"


# ------------------------------------------------------------------ chunking
def _parsed(pages, sections=()):
    return ParsedPdf(pages=[PageText(i + 1, None, t) for i, t in enumerate(pages)], sections=list(sections))


def test_normalize_rejoins_hyphenated_and_wrapped_lines():
    assert normalize("exam-\nple of wrapped\ntext\n\nnext para") == "example of wrapped text\n\nnext para"


def test_chunks_carry_the_right_page_and_respect_size():
    pages = [" ".join(f"word{p}_{i}" for i in range(400)) for p in range(1, 4)]
    chunks = chunk_document(_parsed(pages), size=900, overlap=150, max_chunks=1000)
    assert {c.page for c in chunks} == {1, 2, 3}
    assert all(len(c.text) <= 900 * 1.35 for c in chunks)  # tail-merge can exceed size slightly, never wildly
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert all("word2_" in c.text for c in chunks if c.page == 2 and "word2_" in c.text)
    assert any("word3_" in c.text for c in chunks if c.page == 3)


def test_consecutive_chunks_on_a_page_overlap():
    text = ". ".join(f"Sentence {i} has some filler words to pad it out" for i in range(80))
    chunks = [c for c in chunk_document(_parsed([text]), size=400, overlap=120, max_chunks=1000)]
    assert len(chunks) >= 3
    shared = [set(a.text.split()) & set(b.text.split()) for a, b in zip(chunks, chunks[1:], strict=False)]
    assert all(len(s) > 3 for s in shared)


def test_running_headers_and_page_numbers_are_removed():
    pages = [
        f"ACME CORP CONFIDENTIAL\nReal content for page {i} with enough words to matter here.\n{i}" for i in range(1, 9)
    ]
    cleaned = strip_repeated_lines([PageText(i + 1, None, t) for i, t in enumerate(pages)])
    for p in cleaned:
        assert "CONFIDENTIAL" not in p.text and "Real content" in p.text
        assert not any(ln.strip().isdigit() for ln in p.text.splitlines())


def test_body_sentences_with_a_number_in_the_middle_are_never_treated_as_headers():
    pages = [
        f"Revenue in 2023 grew by {i} percent across all regions.\nSecond line of real prose on page {i}."
        for i in range(8)
    ]
    cleaned = strip_repeated_lines([PageText(i + 1, None, t) for i, t in enumerate(pages)])
    assert all("Revenue in 2023" in p.text for p in cleaned)


def test_header_stripping_only_looks_at_page_edges():
    mid = "Table row: widget count total"  # repeats on every page but sits in the middle, so it is data not a footer
    pages = [
        "\n".join(
            [f"Top line {i} unique words here"]
            + [f"filler {k} {i}" for k in range(3)]
            + [mid]
            + [f"filler {k} {i}" for k in range(3, 6)]
            + [f"last {i} unique end"]
        )
        for i in range(8)
    ]
    cleaned = strip_repeated_lines([PageText(i + 1, None, t) for i, t in enumerate(pages)])
    assert all(mid in p.text for p in cleaned)


def test_sections_come_from_outline_else_from_heading_guess():
    pages = ["intro text " * 30, "middle text " * 30, "late text " * 30]
    c = chunk_document(_parsed(pages, sections=[(1, "Intro"), (3, "Conclusion")]), 900, 150, 100)
    by_page = {ch.page: ch.section for ch in c}
    assert by_page == {1: "Intro", 2: "Intro", 3: "Conclusion"}
    guessed = chunk_document(_parsed(["CHAPTER 2 THE RETURN\n" + "body words " * 40, "more body " * 40]), 900, 150, 100)
    assert {ch.section for ch in guessed} == {"CHAPTER 2 THE RETURN"}  # inherited by the next page too
    assert detect_heading("1.2 Methods and data\nbody") == "1.2 Methods and data"
    assert detect_heading("just an ordinary sentence of body text that goes on and on") is None


def test_sentence_split_across_a_page_break_is_not_lost():
    p1 = "Filler. " * 50 + "The merger was approved by the"
    p2 = "board in March after a long review. Other text follows."
    chunks = chunk_document(_parsed([p1, p2]), size=400, overlap=60, max_chunks=100)
    first_on_p2 = next(c for c in chunks if c.page == 2)
    assert "approved by the board in March" in first_on_p2.text


def test_chunk_cap_and_empty_document_errors():
    with pytest.raises(PdfError) as e:
        chunk_document(_parsed(["word " * 2000]), size=100, overlap=10, max_chunks=5)
    assert e.value.code == "too_many_chunks"
    with pytest.raises(PdfError) as e:
        chunk_document(_parsed(["", "   "]), 900, 150, 100)
    assert e.value.code == "scanned"


def test_five_hundred_page_document_chunks_quickly(tmp_path):
    import time

    pdf = make_pdf([long_lines(f"p{i}", 38) for i in range(500)])
    path = tmp_path / "big.pdf"
    path.write_bytes(pdf)
    t0 = time.time()
    parsed = parse_pdf(path, tmp_path / "w", max_pages=600, timeout_s=170)
    chunks = chunk_document(parsed, 900, 150, 3000)
    assert len(parsed.pages) == 500 and 1000 < len(chunks) <= 3000
    assert chunks[-1].page == 500
    assert time.time() - t0 < 120
