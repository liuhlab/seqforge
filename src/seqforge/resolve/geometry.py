"""Byte-derived geometry feasibility — the winner-invariance foundation for descent scoring.

``length_feasible(spec, wps)`` asks the necessary question the full scorer asks first: can this spec's
reads be assigned, one-to-one, to length-compatible files? It reuses the scorer's own
``read_length_compatible`` gate (:mod:`.evaluators`) and its bipartite matcher (:mod:`.assign`), so a
spec it rejects is one ``build_tech_evaluation`` would also reject — ``_score_cell`` forbids a cell
*unconditionally* on ``read_length_compatible == FAIL`` before any onlist logic runs (scoring.py). That
makes it a **proven necessary condition** for a valid score: narrowing the candidate set to
length-feasible specs can never drop a spec that full scoring would have made a winner. Descent scoring
and the confusability CI guard both stand on this.

``geometry_fingerprint`` is a coarser, dataset-independent key for grouping and diagnostics only;
``length_feasible`` is the correctness predicate.
"""

from __future__ import annotations

import json

from ..kb.schema import SegmentLength, Spec
from .assign import best_assignment
from .evaluators import Outcome, read_length_compatible
from .window import WindowProbe


def length_feasible(spec: Spec, wps: list[WindowProbe]) -> bool:
    """True iff every read can be matched one-to-one to a length-compatible file.

    Uses only ``read_length_compatible`` — the scorer's first, unconditional gate — so a ``False`` here
    provably implies ``build_tech_evaluation(spec, wps, ...).valid is False``. The converse does not
    hold (a length-feasible spec may still be forbidden by a finer requires/excludes test); that is
    correct for a necessary condition and simply means the spec is scored and rejected normally.
    """
    n_roles = len(spec.reads)
    n_files = len(wps)
    if n_files < n_roles:
        return False
    forbidden = [
        [read_length_compatible(read, wp) == Outcome.FAIL for wp in wps] for read in spec.reads
    ]
    score = [[0.0] * n_files for _ in range(n_roles)]
    prior = [[0.0] * n_files for _ in range(n_roles)]
    return best_assignment(n_roles, n_files, score, forbidden, prior).valid


def geometry_could_accept(spec: Spec, probes: list[object]) -> bool:
    """Pairwise necessary condition for ``accepts_at_rungs_0_2(spec, probes)`` (confuse.py).

    Validity requires length-feasibility, so this is a sound skip for the confusability CI guard: a
    ``False`` here means the pair cannot be confusable at rungs 0-2, and the full scorer need not run.
    Mirrors ``accepts_at_rungs_0_2``'s ``WindowProbe`` filtering so it is a drop-in guard.
    """
    wps = [p for p in probes if isinstance(p, WindowProbe)]
    return length_feasible(spec, wps)


def geometry_fingerprint(spec: Spec) -> str:
    """A deterministic, dataset-independent key grouping specs by read geometry (diagnostics only).

    File order is irrelevant (reads are sorted by a canonical descriptor), so two specs that differ
    only in which mate is R1 vs R2 collide. Not a correctness predicate — see :func:`length_feasible`.
    """
    seg_by_read: dict[str, SegmentLength] = {
        t.read: t for t in spec.signature.requires if isinstance(t, SegmentLength)
    }
    reads_desc: list[dict[str, object]] = []
    for read in spec.reads:
        el_types = {el.type for el in read.elements}
        if el_types & {"cdna", "gdna"}:
            kind = f"bio:{read.strand}"
        elif "barcode" in el_types:
            kind = "barcode"
        else:
            kind = "other"
        seg = seg_by_read.get(read.id)
        reads_desc.append(
            {
                "kind": kind,
                "min_len": read.min_len,
                "max_len": read.max_len,
                "seg_length": seg.length if seg else None,
                "over_length_min": seg.over_length_min if seg else None,
                "open_ended": any(
                    el.type in ("cdna", "gdna") and el.end is None for el in read.elements
                ),
            }
        )
    reads_desc.sort(key=lambda d: json.dumps(d, sort_keys=True))
    return json.dumps({"n_reads": len(spec.reads), "reads": reads_desc}, sort_keys=True)
