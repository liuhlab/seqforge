"""The wrong-PDF guard: does a harvested *document* describe a DIFFERENT study than the data?

Sibling to the ``observed_vs_asserted`` chemistry conflicts in :mod:`escalate` — those decide the
*leaf chemistry* from the bytes; this decides *whole-document provenance*. The compile audit (#42,
D2) staged a whole-body-atlas dataset with the wrong paper PDF (a *daf-2* snRNA study). Nothing
caught it; here the compile happened to use GEO records so the wrong PDF was inert, but under a
different dataset a wrong paper silently steers harvest, and a paper↔bytes mismatch is a common,
high-impact error class.

Only a **dataset-scoped** document (a paper/README a human staged) is a wrong-PDF risk — a document
rendered from an archive record is self-consistent with the records by construction, so it is never
checked here. For each paper we compare its own span-verified claims + the accessions it names
against the deterministic records and the byte-observed chemistry:

- **accession** — study-level accessions (GSE / PRJ) the paper's text names vs the dataset's own.
- **organism** — the paper's asserted ``experiment.organism`` (resolved offline to a taxid) vs the
  records' taxid.
- **strain** — the strains/genotypes the paper asserts vs the records' sample strains.
- **assay-vs-bytes** — the chemistry family the paper asserts vs the byte-observed winner.

The refusal is graded, per the design decision on #51:

- any identity signal **mismatches** → an **open** :class:`Conflict` (exit 4, a human decides). A
  paper cites related work, so the bar to surface anything is a positive mismatch, never mere silence.
- the strongest case — **no** identity signal matches, at least one mismatches, **and** the described
  assay family contradicts the bytes → a :class:`Blocker` (exit 3): this is not merely a different
  emphasis, it is a different study.

Nothing here writes a manifest value; it only *withholds* (blocks) or *surfaces* (conflicts). The
accession scan is the one place raw document text is read, and it too only ever gates.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from ..kb.schema import Spec
from ..models.assertion import Assertion
from ..models.blocker import Blocker, BlockerCode, BlockerSubject
from ..models.conflict import Conflict, ConflictPosition
from ..models.records import ArchiveRecordSet
from .confuse import same_family
from .records import DocumentSubject, _organism

Signal = Literal["match", "mismatch", "abstain"]

# Any accession-like token; the study-level subset (GSE / PRJ) is what uniquely names a *study*, so a
# paper about a different study names a different one of these. SRR/SRX/SAMN are collected too (they
# widen the dataset's "own" set, never trigger a mismatch on their own).
_ACCESSION = re.compile(r"\b([SED]R[RXPS]\d+|GS[EM]\d+|PRJ[A-Z]{2}\d+|SAM[NED][A-Z]?\d+)\b")
_STUDY_LEVEL = re.compile(r"\b(GSE\d+|PRJ[A-Z]{2}\d+)\b")

# Tokens too generic to establish a strain/genotype match on their own.
_STRAIN_STOP = frozenset(
    {"wild", "type", "wildtype", "strain", "control", "mutant", "line", "the", "and"}
)


def _significant_tokens(value: str) -> set[str]:
    """Lowercased alphanumeric tokens of a strain/genotype string, minus generic noise."""
    return {
        t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) >= 2 and t not in _STRAIN_STOP
    }


def _own_accessions(records: ArchiveRecordSet) -> set[str]:
    """Every accession the dataset can legitimately call its own — generous on purpose.

    A larger "own" set means fewer false mismatches: if the paper names an accession cross-referenced
    anywhere in our records (a record id, the query, or an id printed in free text / attributes /
    filenames), that counts as the paper naming *us*.
    """
    own = {records.query}
    for r in records.records:
        own.add(r.accession)
        for ft in r.free_text:
            own.update(m.group(0) for m in _ACCESSION.finditer(ft.text))
        for attr in r.attributes:
            own.update(m.group(0) for m in _ACCESSION.finditer(attr.value))
        for fn in r.filenames:
            own.update(m.group(0) for m in _ACCESSION.finditer(fn))
    return {a for a in own if a}


def _record_strains(records: ArchiveRecordSet) -> set[str]:
    strains: set[str] = set()
    for sample in records.at("sample"):
        for key in ("strain", "genotype", "cell_line"):
            value = sample.attribute(key)
            if value:
                strains.add(value)
    return strains


def _accession_signal(text: str, own: set[str]) -> Signal:
    """Does the paper name a study-level accession, and is any of them ours?"""
    named_study = {m.group(0) for m in _STUDY_LEVEL.finditer(text)}
    if not named_study:
        return "abstain"  # many harvested papers print no accession at all
    return "match" if named_study & own else "mismatch"


def _organism_signal(doc_asserts: list[Assertion], rec_taxid: int | None) -> Signal:
    if rec_taxid is None:
        return "abstain"
    from ..io.taxonomy import TaxonomyUnavailable, resolve

    for a in doc_asserts:
        if a.field == "experiment.organism":
            try:
                taxid = resolve(a.value, offline=True)  # seed table only; no network in a gate
            except TaxonomyUnavailable:
                continue  # an organism we cannot resolve offline is no evidence either way
            return "match" if taxid == rec_taxid else "mismatch"
    return "abstain"


def _strain_signal(doc_asserts: list[Assertion], rec_strains: set[str]) -> Signal:
    """Overlap between the strains/genotypes the paper names and the records'.

    Mismatch only when the paper asserts strains, the records carry strains, and NO significant token
    is shared between any pair — the daf-2-paper-over-a-whole-body-dataset (D2) shape. Token overlap,
    not string equality, because a genotype is written a dozen ways.
    """
    doc_values = [
        a.value
        for a in doc_asserts
        if a.field in ("experiment.samples.strain", "experiment.samples.genotype")
    ]
    if not doc_values or not rec_strains:
        return "abstain"
    rec_tokens: set[str] = set()
    for s in rec_strains:
        rec_tokens |= _significant_tokens(s)
    doc_tokens: set[str] = set()
    for value in doc_values:
        doc_tokens |= _significant_tokens(value)
    if not doc_tokens or not rec_tokens:
        return "abstain"
    return "match" if doc_tokens & rec_tokens else "mismatch"


def _assay_contradicts_bytes(
    doc_asserts: list[Assertion], winning_techs: set[str], specs: dict[str, Spec]
) -> bool:
    """The paper asserts a chemistry whose FAMILY no byte-observed winner shares.

    Best-effort and conservative: only fires when the asserted value resolves to a known KB spec id
    (case-insensitive), so an unrecognizable label contributes nothing to the block bar. `same_family`
    encodes the same family-vs-leaf trust the byte conflict detectors use.
    """
    by_id = {sid.lower(): sid for sid in specs}
    for a in doc_asserts:
        if a.field != "library.chemistry":
            continue
        asserted = by_id.get(a.value.strip().lower())
        if asserted is None:
            continue
        if winning_techs and not any(same_family(specs, asserted, w) for w in winning_techs):
            return True
    return False


def check_provenance(
    *,
    assertions: list[Assertion],
    subjects: list[DocumentSubject],
    records: ArchiveRecordSet | None,
    winning_techs: set[str],
    specs: dict[str, Spec],
    document_texts: dict[str, str],
) -> tuple[list[Conflict], list[Blocker]]:
    """Cross-check each dataset-scoped document against the records + observed bytes.

    ``document_texts`` maps a document's ``doc_sha256`` to its canonical text (dataset-scoped docs
    only — the caller supplies these from ``records/documents/``). ``winning_techs`` is the set of
    byte-decided chemistries. Returns ``(open conflicts, blockers)``; both are empty when nothing is
    provided to contradict (no records) or nothing disagrees.
    """
    conflicts: list[Conflict] = []
    blockers: list[Blocker] = []
    if records is None:
        return conflicts, blockers  # no deterministic records to contradict a document against

    scope_of = {s.doc_sha256: s.scope for s in subjects}
    own_acc = _own_accessions(records)
    organism = _organism(records)
    rec_taxid = organism.value if organism else None
    rec_strains = _record_strains(records)

    by_doc: dict[str, list[Assertion]] = defaultdict(list)
    for a in assertions:
        if scope_of.get(a.span.doc_sha256) == "dataset":
            by_doc[a.span.doc_sha256].append(a)

    # Every dataset-scoped document: those with surviving assertions AND those with only text (a wrong
    # PDF whose every claim was rejected still names foreign accessions worth catching).
    dataset_docs = {sha for sha, sc in scope_of.items() if sc == "dataset"} | set(by_doc)

    for sha in sorted(dataset_docs):
        doc_asserts = by_doc.get(sha, [])
        text = document_texts.get(sha, "")

        signals = {
            "accession": _accession_signal(text, own_acc),
            "organism": _organism_signal(doc_asserts, rec_taxid),
            "strain": _strain_signal(doc_asserts, rec_strains),
        }
        if "mismatch" not in signals.values():
            continue  # no positive disagreement — a paper is allowed to be quiet or cite others

        assay_contradicts = _assay_contradicts_bytes(doc_asserts, winning_techs, specs)
        mismatched = sorted(k for k, v in signals.items() if v == "mismatch")
        detail = ", ".join(f"{k}={v}" for k, v in sorted(signals.items()))

        if "match" not in signals.values() and assay_contradicts:
            blockers.append(
                Blocker(
                    id=f"blk-provenance-{sha[:8]}",
                    code=BlockerCode.PROVENANCE_MISMATCH,
                    message=(
                        f"a staged document describes a different study: {detail}, and its assay "
                        "family contradicts the observed reads. No identity signal matches this "
                        "dataset."
                    ),
                    remedy=(
                        "Stage the correct paper/README for this dataset (or drop the document and "
                        "re-run). A wrong document silently steers harvest."
                    ),
                    subject=BlockerSubject(kind="dataset", ref=sha[:12]),
                    evidence=sorted(own_acc)[:8],
                )
            )
            continue

        conflicts.append(
            Conflict(
                id=f"conflict-provenance-{sha[:8]}",
                field="document.provenance",
                kind="other",
                positions=[
                    ConflictPosition(
                        value=f"document claims a different study ({', '.join(mismatched)})",
                        basis="inferred",
                        evidence=[sha[:12]],
                        confidence=1.0,
                    ),
                    ConflictPosition(
                        value="records + observed reads for this dataset",
                        basis="observed",
                        evidence=sorted(own_acc)[:8],
                        confidence=1.0,
                    ),
                ],
                decidable_by=["user"],
                status="open",
            )
        )

    return conflicts, blockers
