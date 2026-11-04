"""Ingestion: turn an uploaded PDF into searchable, page-numbered chunks.

Analogy: the mailroom. A parcel (PDF) arrives, is opened in a sealed side room (a subprocess, so a booby-trapped
file cannot hurt the main program), its pages are listed, cut into index cards (chunks) that remember their
page, and filed for the librarian (retrieval) to find later.
"""
