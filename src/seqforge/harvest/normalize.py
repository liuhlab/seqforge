"""``harvest normalize`` — build the canonical text that spans are computed against (R5).

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

from .fields import DocRole

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
    """

    doc_sha256: str
    normalized_sha256: str
    text: str
    source_basename: str
    role: DocRole = "reference"
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


def read_document(path: Path) -> str:
    """Read a source document to raw text. PDF support is optional and fails loudly, never silently."""
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise RuntimeError(
                f"{path.name}: reading PDF needs `pypdf`, which is not installed. Add it to the "
                "environment, or pre-extract the text and pass the .txt."
            ) from exc
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
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
        n_chars=len(text),
    )
