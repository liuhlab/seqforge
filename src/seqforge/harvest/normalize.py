"""``harvest normalize`` — build the canonical text that spans are computed against.

Span verification is the hallucination tripwire, and it dies on raw PDF text: a naive grep for a
quote fails on soft hyphens, ligatures (``ﬁ`` vs ``fi``), non-breaking spaces, smart quotes, and
mid-sentence line breaks — so a *truthful* quote would be rejected and the tripwire would train us to
ignore it. The fix (brief §12) is to extract **once** into a normalized canonical text, store offsets
into **that**, and verify against **that**.

Deterministic and LLM-free. ``normalizer_version`` is folded into the artifact cache key, because a
normalization change silently invalidates every offset computed under the old one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..models.records import ArchiveRecord
from .fields import DocRole, DocScope

#: CalVer YYYY.M.PATCH; bump when normalization changes (it re-defines the span space).
NORMALIZER_VERSION = "2026.7.0"

# Ligatures NFKC does not decompose the way a grep needs.
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st", "Ĳ": "IJ", "ĳ": "ij", "Œ": "OE", "œ": "oe",
}  # fmt: skip
# Punctuation a PDF renders "prettily" but nobody types when quoting.
_PUNCT = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "′": "'", "″": '"', "­": "",  # prime, double-prime, SOFT HYPHEN (delete)
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "​": "",
}  # fmt: skip

# A hyphen before a line break is ambiguous, and guessing wrong corrupts the span space:
#   "chemi-\nstry"  -> the hyphen is a WRAP artifact  -> close it up  ("chemistry")
#   "3-\nprime"     -> the hyphen is SEMANTIC         -> keep it     ("3-prime")
# Digit-adjacent hyphens (3-prime, R64-1-1, 10-fold) are part of the token, so only an
# alphabetic-alphabetic break is treated as hyphenation to undo.
_WRAP_HYPHEN = re.compile(r"(?<=[A-Za-z])-\n(?=[A-Za-z])")
_KEPT_HYPHEN = re.compile(r"(?<=\w)-\n(?=\w)")
_PARA_BREAK = re.compile(r"\n[ \t]*\n+")  # blank line(s) = a real paragraph boundary
_LINE_BREAK = re.compile(r"[ \t]*\n[ \t]*")  # a lone newline inside a paragraph = a wrap artifact
_WS_RUN = re.compile(r"[ \t]{2,}")
_PARA_TOKEN = "\x00PARA\x00"


@dataclass(frozen=True)
class NormalizedDoc:
    """One document reduced to the canonical span space, with both identities recorded.

    ``doc_sha256`` identifies the SOURCE bytes (stable document identity, what an Assertion cites);
    ``normalized_sha256`` identifies the span space itself, so a normalization drift is detectable.

    ``role`` is what the document IS to us, and it is **not** a property of the bytes: the same PDF is
    a reference when you cite it and an instruction when you write it for us. So it is set by the CLI
    from the flag the document arrived under, and it deliberately does NOT enter ``doc_sha256`` —
    otherwise one file would have two identities and its cached normalization would fork.

    ``scope``/``subject`` are what the document is ABOUT, and they carry the same disclaimer for the
    same reason: code sets them because code chose which record to render, and a document's contents
    never get a vote. ``subject`` is the record's accession at a record scope, and ``None`` for a
    document about the whole dataset. Together they are what lets a claim name a sample while
    ``AssertionDraft`` stays four fields wide.
    """

    doc_sha256: str
    normalized_sha256: str
    text: str
    source_basename: str
    role: DocRole = "reference"
    scope: DocScope = "dataset"
    subject: str | None = None
    normalizer_version: str = NORMALIZER_VERSION
    n_chars: int = 0


def normalize_text(raw: str) -> str:
    """Reduce prose to the canonical form quotes are matched against.

    Order matters: kill soft hyphens and rejoin hyphen-broken words *before* collapsing newlines,
    or the line break that proves a word was split is already gone.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    for src, dst in _LIGATURES.items():
        text = text.replace(src, dst)
    for src, dst in _PUNCT.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKC", text)
    text = _WRAP_HYPHEN.sub("", text)  # "chemi-\nstry" -> "chemistry"
    text = _KEPT_HYPHEN.sub("-", text)  # "3-\nprime"    -> "3-prime" (hyphen is meaningful)
    text = _PARA_BREAK.sub(_PARA_TOKEN, text)  # protect real paragraph boundaries
    text = _LINE_BREAK.sub(" ", text)  # unwrap mid-sentence line breaks
    text = text.replace(_PARA_TOKEN, "\n\n")
    text = _WS_RUN.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


#: Rows rendered per sheet before we stop and say so. A real sample table is tens to low hundreds of
#: rows; anything past this is a data dump (raw counts, a barcode list) beside the metadata, and this
#: text becomes an LLM prompt. The cap is PER SHEET, not per workbook, on purpose: a global budget
#: would let a giant junk sheet 0 starve the sample sheet 3 of room, which is exactly the dirty case.
_MAX_ROWS_PER_SHEET = 500


def _clean_cell(value: object) -> str:
    """One cell -> one contiguous token run.

    A cell is dirty in ways a row layout cannot survive: it may be ``None`` (blank, or a merged cell's
    hidden child), or it may hold its own newlines (``"treated with\\nDMSO"``). Collapsing every
    internal whitespace run to a single space keeps one cell to one token run, so an embedded newline
    can neither shatter the ` | ` columns nor fake a paragraph break in the canonical text.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _read_xlsx(path: Path) -> str:
    """Render EVERY sheet of a workbook to text — because the useful sheet is rarely the first one,
    and the workbook is rarely clean.

    A supplementary `.xlsx` is the common shape of a paper's sample metadata, and it is *plural* and
    *dirty*: the sheet that names the samples ("Sample metadata", "Experimental design") is usually not
    sheet 0, and it sits beside legends, notes, and dumped data. So the one thing this must not do is
    read a single sheet — which is exactly what `pandas.read_excel` does by default (`sheet_name=0`),
    and it would silently drop the sheet we came for. Every sheet is emitted, headed by its name so the
    model can see the book's shape.

    We deliberately do NOT try to find "the" metadata sheet or drop the irrelevant ones: that is
    interpretation, and a wrong guess loses data with no trace. Rendering all of it is cheap, the model
    reads the sheet that matters, and span verification means a dirty cell cannot reach the manifest
    without a quote that greps back to it. Code's only jobs here are mechanical — TRANSCRIBE faithfully
    (no header inference, no dtype coercion — `archive.py`'s discipline), keep each row its own
    paragraph so it survives `normalize_text` (a lone newline is a wrap artifact it flattens; a blank
    line is a boundary it keeps), and bound the size of a dumped sheet **visibly** (`_MAX_ROWS_PER_SHEET`).
    The rendering is deterministic — sheet order, then row/column order — because these bytes are what a
    quote is span-verified against, exactly as for `render_record`.
    """
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for ws in wb.worksheets:
            rows = [f"Sheet: {ws.title}"]
            emitted = 0
            truncated = False
            for row in ws.iter_rows(values_only=True):
                cells = [_clean_cell(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()  # trailing empty cells carry nothing to quote
                if not any(cells):
                    continue  # a blank / spacer row is not a paragraph
                if emitted >= _MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                rows.append(" | ".join(cells))
                emitted += 1
            if truncated:
                # A marked truncation, never a silent one — visible to the model and to a human reading
                # the stored document, so "the table looked short" can never be mistaken for the data.
                rows.append(
                    f"[... more rows in sheet {ws.title!r} omitted after {_MAX_ROWS_PER_SHEET} ...]"
                )
            blocks.append("\n\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(blocks)


def read_document(path: Path) -> str:
    """Read a source document to raw text.

    PDF and XLSX are *extractors* behind the canonical-text contract, not special kinds of input:
    anything else falls through to plain text, so a hand-written `.md` or a `.txt` works with no extra
    code. The contract is the load-bearing part — `normalize_text` produces the one canonical string
    that span verification greps against, whatever the source format was. An `.xlsx` is a zip of XML,
    so it MUST take the extractor branch: read as plain text it would be replacement-char garbage, and
    a truthful quote could never grep back.

    `pypdf`/`openpyxl` are imported lazily because these are the uncommon cases, but both are **declared
    dependencies** now, so the import does not fail. pypdf was once undeclared, with a remedy telling
    the user to install it by hand — which meant no supported install of seqforge could read a paper,
    the one document type the pilot dataset actually ships; openpyxl carries the same weight for the
    supplementary tables papers ship their sample metadata in.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if suffix in {".xlsx", ".xlsm"}:
        return _read_xlsx(path)
    return path.read_text(encoding="utf-8", errors="replace")


def normalize_document(path: str | Path, *, role: DocRole = "reference") -> NormalizedDoc:
    """Turn one source document into its :class:`NormalizedDoc` (the canonical span space).

    ``role`` comes from the caller — i.e. from which CLI flag the document arrived under — because it
    is a fact about how the document was OFFERED, not about its contents. Never infer it from the
    filename: that would be spoofable by renaming a downloaded PDF.
    """
    p = Path(path)
    source_bytes = p.read_bytes()
    text = normalize_text(read_document(p))
    return NormalizedDoc(
        doc_sha256=hashlib.sha256(source_bytes).hexdigest(),
        normalized_sha256=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        source_basename=p.name,
        role=role,
        # A document a human handed us is about the whole pile of files. There is no other honest
        # reading of "here is the paper" — see `resolve/records.py` for what that costs a sample claim.
        scope="dataset",
        n_chars=len(text),
    )


def render_record(record: ArchiveRecord) -> str:
    """One archive record -> the text a model reads. Deterministic, and the ONLY renderer.

    Deterministic matters more than it looks. This text *is* the document: its sha256 is the identity
    an assertion cites, and the span check greps this exact string. So the rendering must be
    reproducible from the record forever — a human handed the record and this function must be able to
    regenerate the bytes a quote was checked against, or the citation is unfalsifiable.

    Only free text is rendered. The structured half (``strain = CQ758``) is code's to read and is
    already a key and a value, so putting it in front of a model would be asking it to transcribe
    something we can copy — a chance to be wrong and no chance to be useful.
    """
    lines = [f"{record.level} {record.accession}"]
    for ft in record.free_text:
        lines.append(f"{ft.label}: {ft.text}")
    return "\n\n".join(lines)


def normalize_record(record: ArchiveRecord) -> NormalizedDoc:
    """An archive record -> its own document, scoped to itself.

    This is the whole mechanism behind "a claim can name a sample without being able to name a
    sample". The document holds one record's prose, so whatever the model finds in it is about that
    record, because that is the only thing in it. ``subject`` is set from the record we rendered —
    code knows it because code chose it, exactly as ``instruct.py`` decides document role.
    """
    text = normalize_text(render_record(record))
    digest = hashlib.sha256(text.encode()).hexdigest()
    return NormalizedDoc(
        # The rendering IS the source: there are no other bytes to identify. Both hashes are the same
        # string's, and saying so is more honest than inventing a second identity for one document.
        doc_sha256=digest,
        normalized_sha256=digest,
        text=text,
        source_basename=f"{record.level}-{record.accession}.txt",
        role="reference",  # an archive record is a database field, never an instruction to us
        scope=record.level,
        subject=record.accession,
        n_chars=len(text),
    )


def has_prose(record: ArchiveRecord) -> bool:
    """Is there anything here for a model to read? A record with no free text is not worth a call."""
    return any(ft.text.strip() for ft in record.free_text)
