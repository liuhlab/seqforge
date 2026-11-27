"""What harvest leaves in a workspace, written and read here: the assertions file and the documents.

The module that writes an artifact owns reading it (ADR-0025). Harvest writes two things a later
process opens: ``logs/assertions.json``, the verified claims with which document each one cites and
what each document is about, and ``records/documents/``, the canonical text each quote greps back
into. A reader elsewhere — the report — goes through the functions here, so renaming either path
breaks a harvest test rather than quietly emptying a report panel.

The readers are tolerant, because the report degrades rather than refuses: a missing or unreadable
file reads as empty. They also read the top-level layout an earlier build wrote, ``seqforge/
assertions.json`` and ``seqforge/documents/``, and the bare-list form of the assertions file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ..workspace import documents_dir, logs_dir, readable, state_dir

if TYPE_CHECKING:
    from ..models.assertion import Assertion
    from .normalize import NormalizedDoc

#: The assertions file's name, under ``logs/``.
ASSERTIONS_FILENAME = "assertions.json"

#: A document file's stem ends in ``-<first 12 hex of its doc_sha256>``; see :func:`document_filename`.
_HEX12 = re.compile(r"-([0-9a-f]{12})$")


class HarvestLog(NamedTuple):
    """The assertions file as JSON: the claims, each document's scope, and the instruction docs."""

    assertions: list[dict[str, Any]]
    document_subjects: list[dict[str, Any]]
    instruction_docs: list[str]


def assertions_path(workspace: str | Path) -> Path:
    """``seqforge/logs/assertions.json``, where :func:`write_assertions` writes."""
    return logs_dir(workspace) / ASSERTIONS_FILENAME


def document_filename(doc: NormalizedDoc) -> str:
    """``paper.pdf`` -> ``paper-3f8a1c2d9b04.txt``; a record -> ``experiment-SRX22069914-....txt``.

    The hash stays, because two documents can share a name and the identity is the hash. But a
    directory of bare 64-hex filenames is a directory you cannot read, and an early build's document
    cache was exactly that: nothing in it said which file was the paper. The source
    name is already known -- we opened the file -- so printing it costs nothing and no model is
    involved in producing it.
    """
    return readable(Path(doc.source_basename).stem, doc.doc_sha256) + ".txt"


def write_documents(workspace: str | Path, docs: Iterable[NormalizedDoc]) -> list[Path]:
    """Write each document's canonical text under ``records/documents/``; return the paths in order."""
    docdir = documents_dir(workspace)
    docdir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for doc in docs:
        target = docdir / document_filename(doc)
        target.write_text(doc.text)
        paths.append(target)
    return paths


def write_assertions(
    workspace: str | Path,
    *,
    assertions: Iterable[Assertion],
    documents: Iterable[NormalizedDoc],
    instruction_docs: Iterable[str],
) -> Path:
    """Write the assertions file: the claims, each document's scope and subject, the instruction docs.

    An object and not a bare list, because which documents were authored for seqforge and which
    record each document was rendered from are what later stages need to reconstruct, and they live
    nowhere else once this process ends.
    """
    path = assertions_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "instruction_docs": sorted(instruction_docs),
                "document_subjects": [
                    {"doc_sha256": d.doc_sha256, "scope": d.scope, "subject": d.subject}
                    for d in sorted(documents, key=lambda d: d.doc_sha256)
                ],
                "assertions": [a.model_dump(mode="json") for a in assertions],
            },
            indent=2,
        )
    )
    return path


def read_assertions(workspace: str | Path) -> HarvestLog:
    """The assertions file harvest wrote, or an empty log if there is none or it cannot be read."""
    for path in (assertions_path(workspace), state_dir(workspace) / ASSERTIONS_FILENAME):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, list):
            data = {"assertions": data}
        if not (isinstance(data, dict) and isinstance(data.get("assertions"), list)):
            continue
        return HarvestLog(
            assertions=[a for a in data["assertions"] if isinstance(a, dict)],
            document_subjects=[d for d in data.get("document_subjects", []) if isinstance(d, dict)],
            instruction_docs=[str(d) for d in data.get("instruction_docs", [])],
        )
    return HarvestLog(assertions=[], document_subjects=[], instruction_docs=[])


def document_labels(workspace: str | Path) -> dict[str, str]:
    """``{doc_sha256[:12] -> readable stem}`` over the documents on disk, in both layouts.

    The stem is the file's name with its ``-<hash12>`` cut, such as ``experiment-SRX24283130``.
    """
    labels: dict[str, str] = {}
    for d in (documents_dir(workspace), state_dir(workspace) / "documents"):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.txt")):  # sorted: a stable label under any filesystem order
            m = _HEX12.search(f.stem)
            if m:
                labels.setdefault(m.group(1), f.stem[: m.start()])
    return labels
