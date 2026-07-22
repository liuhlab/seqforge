"""Per-read window resolution for **anchored / variable-position** elements (design §2.1, §9/§10).

Most chemistries put every element at a fixed ``[start, end)`` and the scorer slices a constant column.
A few do not: BD Rhapsody's *Enhanced* bead prepends a **variable 0-3 bp diversity insert** to Read 1,
so every cell-label block downstream **staggers** by a per-read amount and no single column holds a
barcode. STARsolo handles this with an *adapter anchor* (find the ``GTGA…GACA`` linker frame, read the
barcodes relative to it); this module is seqforge's byte-side twin of that idea.

``resolve_windows(seq, read)`` locates each element's ``[start, end)`` **in one read** by
**phase detection**: the only unknowns are the lengths of the variable-length elements (the insert),
so enumerate their candidate lengths, walk the element chain for each, and keep the phase whose
``linker``/``fixed`` elements Hamming-match their declared ``sequence`` (substitutions only, exactly
the error model rhapsodist uses across its 0-3 bp offset scan). A read whose frame cannot be found
(a cDNA read, a garbage read) returns ``None`` and simply does not contribute — never a wrong slice.

This lives in ``kb`` (not ``resolve`` or ``probe``) on purpose: it is pure layout logic over a
``kb.schema.Read`` and a string, and both ``kb.roundtrip`` (which cannot import ``resolve`` — that is a
cycle) and ``resolve.window`` need it.
"""

from __future__ import annotations

from itertools import product

from .schema import Element, Read

#: Substitutions tolerated when Hamming-matching a linker whose element declares no ``anchor``
#: ``max_mismatch``. One per short linker absorbs ordinary sequencing error without letting a wrong
#: frame match (two independent linkers must BOTH match at the right spacing to lock a phase).
_DEFAULT_LINKER_MAX_MISMATCH = 1


def has_anchored_elements(read: Read) -> bool:
    """True iff any element on this read floats (declares an ``anchor``) — the resolver's trigger."""
    return any(el.anchor is not None for el in read.elements)


def _fixed_width(el: Element) -> int | None:
    """The element's width when it is known up front; ``None`` for a variable-length / open element."""
    if el.start is not None and el.end is not None:
        return el.end - el.start
    if el.sequence is not None:
        return len(el.sequence)
    if el.min_len is not None and el.min_len == el.max_len:
        return el.min_len
    return None


def _variable_elements(read: Read) -> list[Element]:
    """Elements whose length is an unknown to solve — a declared ``[min_len, max_len]`` with min<max.

    These are the phase's degrees of freedom (BD Enhanced has exactly one: the 0-3 bp insert). An
    open-ended cDNA/gDNA tail is NOT one of these — it has no upstream effect on the barcode frame, so
    it is resolved last, running to the end of the read.
    """
    out: list[Element] = []
    for el in read.elements:
        if el.type in ("cdna", "gdna") and el.end is None:
            continue
        if el.min_len is not None and el.max_len is not None and el.min_len < el.max_len:
            out.append(el)
    return out


def _candidate_assignments(read: Read, *, cap: int = 256) -> list[dict[str, int]]:
    """Every combination of lengths for the variable elements, as ``{element_name: length}``.

    Capped so a pathological spec cannot make resolution exponential; BD Enhanced yields 4 candidates
    (insert length 0-3). Returns ``[{}]`` (the single empty assignment) when nothing is variable, so a
    purely motif-anchored fixed-width chemistry still resolves.
    """
    variable = _variable_elements(read)
    if not variable:
        return [{}]
    ranges = [range(el.min_len or 0, (el.max_len or 0) + 1) for el in variable]
    names = [el.name for el in variable]
    combos = list(product(*ranges))
    if len(combos) > cap:
        combos = combos[:cap]
    return [dict(zip(names, lengths, strict=True)) for lengths in combos]


def _anchor_start(el: Element, windows: dict[str, tuple[int, int]], prev_end: int) -> int:
    """Where this element begins, from its ``anchor`` (falling back to the previous element's end).

    The chain BD Enhanced declares is sequential — each element anchored to the previous element's
    ``end`` with ``offset 0`` — so resolving in element order means every ``ref_element`` is already
    placed, and this reduces to a cumulative walk. Honouring the anchor rather than assuming adjacency
    keeps a non-zero ``offset`` or a ``read_start``-relative element correct too.
    """
    a = el.anchor
    if a is None:
        return el.start if el.start is not None else prev_end
    if a.relative_to == "element" and a.ref_element and a.ref_element in windows:
        ref = windows[a.ref_element]
        base = ref[0] if a.ref_side == "start" else ref[1]
        return base + a.offset
    if a.relative_to == "read_start":
        return a.offset
    # read_end, or an unresolved ref: fall back to the running position (the chain never needs this).
    return prev_end + a.offset


def _resolve_for_assignment(
    seq: str, read: Read, assignment: dict[str, int]
) -> tuple[dict[str, tuple[int, int]], int, bool] | None:
    """Place every element for one candidate length assignment.

    Returns ``(windows, linker_mismatches, all_linkers_matched)`` or ``None`` if the layout does not
    fit the read (a window runs off the 3' end). ``linker_mismatches`` is the total Hamming distance
    across all ``linker``/``fixed`` elements — the smaller, the better the frame fits.
    """
    windows: dict[str, tuple[int, int]] = {}
    pos = 0
    mismatches = 0
    all_matched = True
    n = len(seq)
    elements = read.elements
    for i, el in enumerate(elements):
        start = _anchor_start(el, windows, pos)
        width = assignment.get(el.name)
        if width is None:
            width = _fixed_width(el)
        if width is None:
            # open-ended element (cDNA tail): runs to the end of the read; must be terminal.
            if i != len(elements) - 1:
                return None
            end = n
        else:
            end = start + width
        if start < 0 or end > n:
            return None  # this phase pushes an element off the read
        windows[el.name] = (start, end)
        if el.sequence is not None:  # a linker/fixed element anchors the frame
            tol = el.anchor.max_mismatch if el.anchor is not None else _DEFAULT_LINKER_MAX_MISMATCH
            d = _hamming(seq[start:end], el.sequence)
            mismatches += d
            if d > tol:
                all_matched = False
        pos = end
    return windows, mismatches, all_matched


def resolve_windows(seq: str, read: Read) -> dict[str, tuple[int, int]] | None:
    """Locate each element's ``[start, end)`` in ``seq`` by phase detection; ``None`` if not found.

    Picks the candidate phase whose linker/fixed elements all Hamming-match their declared sequence
    (ties broken by fewest total mismatches). Requires at least one linker to anchor on — with none,
    the frame is unobservable and the read is left unresolved rather than sliced arbitrarily.
    """
    if not any(el.sequence is not None for el in read.elements):
        return None
    best: tuple[int, dict[str, tuple[int, int]]] | None = None
    for assignment in _candidate_assignments(read):
        resolved = _resolve_for_assignment(seq, read, assignment)
        if resolved is None:
            continue
        windows, mismatches, all_matched = resolved
        if not all_matched:
            continue
        if best is None or mismatches < best[0]:
            best = (mismatches, windows)
    return best[1] if best is not None else None


def _hamming(a: str, b: str) -> int:
    """Substitution distance over the compared length; length mismatch counts as all-different tail."""
    d = sum(1 for x, y in zip(a, b, strict=False) if x != y)  # short-zip: tail counted below
    return d + abs(len(a) - len(b))
