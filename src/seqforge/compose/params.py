"""The **params gate** — the semantic assertions a dry-run cannot make.

``snakemake -n`` proves the *wiring*; it cannot see that ``--soloUMIlen`` is 10 when the reads carry a
12 bp UMI, or that ``--readFilesIn`` puts the barcode read where the cDNA read belongs. Those are the
bugs a config compiler actually produces, and they fail **silently**: STARsolo exits 0 and emits a
matrix that merely looks like a thin dataset. So they get deterministic assertions of their own, run
on every compose, with no data and no aligner.

Every emitted aligner param has exactly **one owner**, and this gate is where that stops being
a convention:

- the **KB** owns how to PARSE reads — soloType, CB/UMI offsets, whitelist, strand. Byte-decided.
- the **processing manifest** owns what to COUNT — soloFeatures, quantMode. Instructable.

Four checks:

1. **Disjointness** — the two owners' key sets never intersect. This is what makes "a user instruction
   contradicts the observed bytes" *inexpressible* rather than merely deprioritized.
2. **Coverage / no orphan** — the emitted key set is EXACTLY the union of the two. Disjointness alone
   is the decorative-``quantification`` bug in reverse: it proves the two sources cannot disagree, not
   that either key actually *arrives*. Requiring the exact union means every emitted key is
   attributable to one owner and every declared key is emitted — so a key that MOVES between owners is
   caught by whichever side forgot it. Before this, the gate iterated the KB alone, and a key moved out
   of the KB silently stopped being gated at all.
3. **Faithfulness, per key, per owner** — KB keys verbatim from the spec; processing keys verbatim from
   the rendered manifest value. This is what stops ``processing.quantification`` being decorative:
   policy used to write it to the manifest and compose ignored it, reading the KB instead — two sources
   of truth for one decision, unable to disagree only because one was never consulted.
4. **Cross-derivation** — the KB's declared offsets/lengths agree with the *observed* read layout
   (catches a KB whose params contradict the bytes: ``soloCBlen 16`` over a 12 bp CB).
5. **Pairwise legality** — a param whose legal values depend on another param's value is checked as a
   PAIR, not as two independent strings. Today that is ``(soloType, soloCBmatchWLtype)``: an illegal
   combination is a hard STAR FATAL, and STAR raises it *after* the genome loads, on a compute node,
   minutes into a job. A gate check turns that into a compose-time refusal, which is precisely this
   gate's stated job.
6. **readFilesIn** — each read role maps to the byte-decided read, per the pipeline's layout kind.

Strand correctness itself is NOT decidable here — only the `kb e2e` count-matrix run can catch an
inverted ``--soloStrand``. This gate asserts the value survives compose intact; the e2e asserts it is
*right*.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal, assert_never

from ..kb.schema import Element, Read, Spec
from ..models.dataset import DatasetManifest, ReadDef, ReadElement
from ..models.processing import BulkQuant, ProcessingManifest, Quantification, SoloQuant
from ..workflows import ReadLayoutKind, get_module, parse_keys_for

GateStatus = Literal["pass", "fail"]
ParamOwner = Literal["kb", "processing", "derived"]

RECIPE_PARAM_KEYS: frozenset[str] = frozenset({"soloFeatures", "quantMode"})
"""Every backend param sourced from the processing manifest. Each says what to **COUNT**."""

CB_MATCH_WL_TYPES: dict[str, frozenset[str]] = {
    "CB_UMI_Simple": frozenset(
        {"Exact", "1MM", "1MM_multi", "1MM_multi_pseudocounts", "1MM_multi_Nbase_pseudocounts"}
    ),
    "CB_UMI_Complex": frozenset({"Exact", "1MM", "EditDist_2"}),
}
"""Which ``--soloCBmatchWLtype`` values STAR accepts, **per soloType**.

Measured, not read off ``--help``: all six values were run against the STAR 2.7.11b binary under both
soloTypes, because ``--help`` lists the six as one flat menu and says nothing about the split. The two
sets overlap in ``Exact``/``1MM`` and are otherwise disjoint — the ``1MM_multi*`` family is
Simple-only, ``EditDist_2`` is Complex-only — and STAR's refusal is a hard ``EXITING because of fatal
PARAMETERS error``, not a warning it proceeds past.

The value that makes this worth gating rather than documenting: **STAR's own global default,
``1MM_multi``, is illegal for ``CB_UMI_Complex``.** So the failure is not "someone typed an exotic
value"; it is "a Complex chemistry said nothing", which is the easiest thing in the world for a new
spec to do. It is also the failure class this repo names explicitly — every wrong answer here breaks
the four Complex chemistries and leaves all seven 10x ones green.

A soloType that is not a key here is refused rather than waved through. STAR has others (``SmartSeq``,
``CB_samTagOut``) and ``starsolo.smk`` emits ``--soloCBmatchWLtype`` unconditionally, so a spec
declaring one would be a contract we have not established — the same "must not be guessed at" line
:attr:`~seqforge.workflows.WorkflowModule.param_block` draws.
"""

DERIVED_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "soloCBposition",
        "soloUMIposition",
        "soloAdapterSequence",
        "read_format",
        "read_structure",
        "read_through",
    }
)
"""Params computed from the element model rather than declared by anyone.

Still parse keys — byte-decided, never instructable — but the bytes already answered them in
the spec's element coordinates, so a KB that *also* declared the quadruple would carry the same fact
twice and let the two drift. A third owner, because "one fact, one owner" is the whole point of
:func:`param_owners`; folding these into ``kb`` would make the gate certify a value the KB never
stated.

``read_format`` is chromap's analog of the STARsolo position quadruple: the cell barcode's
within-read placement (start, width) and strand, read off the CB element and the barcode onlist's
orientation. 10x Multiome ATAC carries the 16 bp barcode behind an 8 bp lead-in and reverse-complemented
vs the whitelist, so ``--read-format bc:8:23:-`` is what chromap needs — and, being a fact the element
coordinates already state, it is derived here rather than declared in ``backend.params``.

``soloAdapterSequence`` joined this set for BD Rhapsody Enhanced (#43): an anchored chemistry's
diversity insert is absorbed by STARsolo's adapter anchor, and the adapter (``NNN…GTGANNN…GACA``) is
just the barcode widths and linker literals read off the elements — one more fact the coordinates
already state. It was in the pipeline's parse keys (declarable) but nothing emitted it; now it is derived, and
the ``soloCBposition``/``soloUMIposition`` quadruples become adapter-anchored (anchor 2/3) rather than
read-start-anchored (anchor 0) for such a chemistry.

``read_structure`` is the plate assay's whole extraction geometry — which read carries the tag, the
tag itself and where it is declared, the UMI's offset and width, the motif that closes the tag, and
where cDNA begins. It is the ``soloAdapterSequence`` precedent one chemistry later and taken further:
the extractor needs SIX numbers and every one of them is already in the element coordinates, so
``map/star-umi``'s parse namespace is **empty** and this key carries all of them at once. One key
rather than six, exactly as ``read_format`` renders chromap's barcode placement — six travelling
separately is five that arrive and one that is dropped, and five correct numbers still cut *a* span
out of *a* read at exit 0.

``read_through`` is the odd one, and deliberately: it is the only key here whose VALUE the chemistry
states outright rather than one computed from coordinates. What is derived is everything else — that
this pipeline can clip at all, and the arity its aligner demands. That split is the point. The
sequence is a fact about the molecule and belongs to the entry, while how a particular aligner is
told about it belongs to the module, so the entry says it once and every consumer works out its own
flag (ADR-0048). Declaring it obliges the pipeline to honour it: a chemistry naming an adapter
nothing clips is worse off than one naming none, because the adapter then sits inside STAR's
length-relative filter and is counted against the read it is not part of. :func:`params_gate`
refuses that pairing.
"""


def derived_params(spec: Spec) -> dict[str, str]:
    """Locate a ``CB_UMI_Complex`` chemistry's barcodes/UMI from its elements, as STAR wants them.

    STARsolo's complex chemistries take position quadruples
    (``startAnchor_startPos_endAnchor_endPos``; positions 0-based INCLUSIVE) rather than the
    start/length pair a simple chemistry uses. The splitseq spec says outright why this is computed
    and not written down: *"never hand-enter a position quadruple from memory — generate it from the
    element model"*. A published quadruple is also chemistry-specific in a way that invites exactly
    that error — v1's Round1 sits at 86-93 and Parse/v2's at 78-85, so a remembered value is a coin
    flip between two real chemistries.

    Two geometries, one function. A **fixed-offset** chemistry anchors every element to the read start
    (anchor 0): ``0_<start>_0_<end>``. An **anchored** chemistry (BD Rhapsody Enhanced's floating
    diversity insert) cannot — no offset is constant — so it anchors to the ``GTGA…GACA`` adapter
    instead (anchor 2 = adapter start, anchor 3 = adapter end), and also derives the
    ``soloAdapterSequence`` STARsolo locates that adapter by. Both are read off the same element model;
    which one applies is decided by whether the barcode read carries an ``anchor``.

    Order is load-bearing: STARsolo pairs the Nth ``soloCBwhitelist`` with the Nth
    ``soloCBposition``, so the quadruples are emitted in the whitelist's declared order, never the
    elements' positional order.
    """
    backend = spec.require_backend()
    # chromap expresses barcode geometry through `--read-format`, the way STARsolo expresses it through
    # the position quadruple below — dispatched on the module's declared `param_block`, never its name
    # (a name compare is the `_read_files_in` bug this file's guard forbids).
    block = get_module(backend.module).param_block
    if block == "chromap":
        return _chromap_read_format(spec)
    if block == "umi":
        return _umi_read_structure(spec) | _read_through(spec)
    if backend.params.get("soloType") != "CB_UMI_Complex":
        return {}

    by_onlist: dict[str, Element] = {}
    umi: Element | None = None
    bc_read = None
    for read in spec.reads:
        for el in read.elements:
            if el.type == "barcode" and el.onlist:
                by_onlist[el.onlist] = el
                bc_read = read
            elif el.type == "umi":
                umi = el

    aliases = _whitelist_aliases(backend.params.get("soloCBwhitelist"))
    out: dict[str, str] = {}

    if bc_read is not None and any(el.anchor is not None for el in bc_read.elements):
        frame = _adapter_frame(bc_read)
        if frame is None:
            return {}  # no linker anchor to hang the adapter on — nothing safe to derive
        adapter_seq, quad = frame
        out["soloAdapterSequence"] = adapter_seq
    else:
        quad = _quadruple

    positions = [q for a in aliases if (q := quad(by_onlist.get(a))) is not None]
    if positions:
        out["soloCBposition"] = " ".join(positions)
    umi_pos = quad(umi)
    if umi_pos is not None:
        out["soloUMIposition"] = umi_pos
    return out


def _chromap_read_format(spec: Spec) -> dict[str, str]:
    """chromap's ``--read-format bc:START:END:STRAND`` for a barcoded ATAC chemistry, from the CB element.

    The barcode's within-read placement is byte-decided and already stated in the element coordinates —
    exactly like STARsolo's ``soloCBposition`` — so it is DERIVED here, never declared in
    ``backend.params``. ``START``/``END`` are 0-based **inclusive** (chromap's convention; the element
    model is half-open ``[start, end)``, hence ``end - 1``). ``STRAND`` is ``-`` when the barcode is
    carried reverse-complemented vs the whitelist (the onlist's ``expected_orientation``) — 10x Multiome
    ATAC's case, where the 16 bp barcode sits behind an 8 bp lead-in and matches the ARC ATAC list only
    reverse-complemented — else ``+``. Only ``bc`` is emitted: the two genomic mates are full-length, so
    chromap's default ``r1``/``r2`` framing already reads them end to end.
    """
    cb: Element | None = None
    for read in spec.reads:
        for el in read.elements:
            if el.type == "barcode" and el.start is not None and el.end is not None:
                cb = el
    if cb is None or cb.start is None or cb.end is None:
        return {}
    ref = spec.onlists.get(cb.onlist) if cb.onlist else None
    strand = "-" if (ref is not None and ref.expected_orientation == "revcomp") else "+"
    return {"read_format": f"bc:{cb.start}:{cb.end - 1}:{strand}"}


def _umi_read_structure(spec: Spec) -> dict[str, str]:
    """The plate assay's whole extraction geometry, as ONE derived key, from the element model.

    ``map/star-umi``'s parse namespace is empty, so this is the only thing in its config block and
    it carries everything the extractor needs: which read is tagged, the tag and where the layout
    declares it, the UMI's offset and width, the motif that closes the tag, and where cDNA begins.
    Six facts, one value — six keys travelling separately is five that arrive and one that is
    dropped, and five correct numbers still cut *a* span out of *a* read at exit 0.

    The derivation itself is the EXTRACTOR's, called here rather than reproduced: the spec's reads
    are translated through the one KB→IR element translator and handed to the same walker the
    manifest side uses, so "what the chemistry declares" and "what the bytes were decided to be"
    cannot be two answers. The gate then re-derives the second and compares.

    Returns ``{}`` for a spec whose layout is not this shape at all — a chemistry that declares no
    UMI element, or more than one tagged read. Silently emitting a half-geometry would be worse than
    emitting none: the params gate's coverage check turns a missing key into a named refusal, while
    a wrong one is a run that extracts from the wrong bases.
    """
    from ..manifest.fill import declared_read_elements
    from ..workflows.umite.extract import UmiExtractError, tagged_geometry

    try:
        geometry = tagged_geometry(declared_read_elements(spec))
    except UmiExtractError:
        return {}
    return {"read_structure": geometry.render()}


def _read_through(spec: Spec) -> dict[str, str]:
    """The adapter a short fragment runs off the end of its cDNA into, for a pipeline that clips it.

    Emitted only from the branch of :func:`derived_params` whose pipeline can actually perform the
    clip, which is what makes "declared but unhonoured" visible to the gate rather than silent: the
    key's absence from a chemistry that declares one IS the refusal. No pipeline carries a list of
    the sections it honours — such a list is a second statement of what its own composer emits, free
    to drift from it, and the drift would read as a chemistry being clipped when it is not.

    The sequence passes through verbatim, because unlike every other derived key there is no
    coordinate to compute it from. What keeps the clip off a read that must not have it is the ROLE
    placement this same gate already checks: the aligner is handed the reads compose placed as cDNA,
    and a barcode read is never among them.
    """
    return {"read_through": spec.read_through} if spec.read_through else {}


def _whitelist_aliases(whitelist: object) -> list[str]:
    """The ``{onlist:alias}`` tokens of ``soloCBwhitelist``, in declared (CB-position) order."""
    values = whitelist if isinstance(whitelist, list) else [whitelist]
    return [
        v[len("{onlist:") : -1] for v in values if isinstance(v, str) and v.startswith("{onlist:")
    ]


def _quadruple(el: Element | None) -> str | None:
    """One FIXED-offset element -> ``0_<start>_0_<end>``: anchored at the read start, ends inclusive.

    The element model is half-open ``[start, end)`` (Python's convention); STAR's quadruple is
    closed. That off-by-one is the whole reason this is a function with a name.

    ``None`` when the element is absent or open-ended: a quadruple needs both coordinates, and an
    element without them (cDNA runs to the end of the read, OR an anchored element that floats) has no
    fixed position to state. Returning ``None`` keeps the key out of the config entirely rather than
    emitting ``0_0_0_-1``, which STAR would accept as a real and wrong instruction.
    """
    if el is None or el.start is None or el.end is None:
        return None
    return f"0_{el.start}_0_{el.end - 1}"


def _nominal_width(el: Element) -> int | None:
    """An element's constant width (diversity insert at its MINIMUM), or ``None`` if open-ended.

    The adapter-anchored quadruples are invariant to the diversity insert's per-read length (every
    element shifts together), so they are computed in NOMINAL coordinates — the layout with the insert
    at its minimum. That is what makes ``2_0_2_8`` a single derivable fact rather than a per-read one.
    """
    if el.start is not None and el.end is not None:
        return el.end - el.start
    if el.sequence is not None:
        return len(el.sequence)
    if el.min_len is not None:
        return el.min_len
    return None


def _adapter_frame(read: Read) -> tuple[str, Callable[[Element | None], str | None]] | None:
    """Build STARsolo's adapter sequence + an adapter-anchored quadruple maker for a floating chemistry.

    The adapter spans from the first barcode/UMI/linker element through the LAST linker (BD Enhanced:
    ``CLS1 GTGA CLS2 GACA``), rendered as ``N``×width for a barcode/UMI and the literal for a linker ->
    ``NNNNNNNNNGTGANNNNNNNNNGACA``. STARsolo finds that in each read, absorbing the leading diversity
    insert. Elements up to the last linker are then anchored to the adapter START (anchor 2); elements
    after it (CLS3, UMI) to the adapter END (anchor 3, where position 0 is the adapter's last base).
    ``None`` when there is no linker to anchor on. All coordinates are NOMINAL (:func:`_nominal_width`).
    """
    order = list(read.elements)
    nominal: dict[str, tuple[int, int]] = {}
    pos = 0
    for el in order:
        w = _nominal_width(el)
        if w is None:
            return None  # an open-ended element in the barcode read: not an adapter chemistry
        nominal[el.name] = (pos, pos + w)
        pos += w

    linker_idxs = [
        i for i, el in enumerate(order) if el.type in ("linker", "fixed") and el.sequence
    ]
    adapter_idxs = [
        i for i, el in enumerate(order) if el.type in ("barcode", "umi", "linker", "fixed")
    ]
    if not linker_idxs or not adapter_idxs:
        return None
    start_idx, last_linker_idx = adapter_idxs[0], linker_idxs[-1]

    adapter_seq = "".join(
        el.sequence
        if (el.type in ("linker", "fixed") and el.sequence)
        else "N" * (nominal[el.name][1] - nominal[el.name][0])
        for el in order[start_idx : last_linker_idx + 1]
    )
    adapter_start = nominal[order[start_idx].name][0]
    adapter_end = nominal[order[last_linker_idx].name][1]  # one-past the adapter's last base

    def quad(el: Element | None) -> str | None:
        if el is None or el.name not in nominal:
            return None
        s, e = nominal[el.name]
        width = e - s
        if width <= 0:
            return None
        if s >= adapter_end:  # after the adapter -> anchor 3 (position 0 == adapter's last base)
            rel = s - (adapter_end - 1)
            return f"3_{rel}_3_{rel + width - 1}"
        rel = s - adapter_start  # within the adapter -> anchor 2 (relative to adapter start)
        return f"2_{rel}_2_{rel + width - 1}"

    return adapter_seq, quad


def processing_params(quant: Quantification) -> dict[str, object]:
    """Render a counting decision into the aligner params it stands for.

    Module-scoped by construction: ``soloFeatures`` is meaningless to plain STAR and ``quantMode`` is
    meaningless to STARsolo, so the discriminated union is what keeps a processing manifest from being
    a type error the moment it meets the other module.
    """
    if isinstance(quant, SoloQuant):
        # space-joined, exactly as the KB's list rendering did — STAR takes repeated argv values
        return {"soloFeatures": " ".join(quant.features)}
    if isinstance(quant, BulkQuant):
        return {"quantMode": quant.mode}
    # AtacQuant and UmiQuant: neither has a counting knob to render, for two different reasons that
    # land in the same place. ATAC's deliverable is a fragments file, so there is nothing to count;
    # the plate counter writes every matrix in one pass, so there is nothing to choose. The
    # empty dict keeps `param_owners`/`params_gate` correct — each of those two config blocks is
    # exactly its own keys, with no processing-owned counting key to reconcile.
    return {}


def param_owners(spec: Spec, processing: ProcessingManifest) -> dict[str, ParamOwner]:
    """Every emittable aligner param key -> the artifact entitled to set it.

    The parse/count line as a **computed fact**, directly unit-testable, rather than a comment nobody
    re-reads. A key with two owners, or with none, is a bug this function surfaces and the gate fails
    on.
    """
    owners: dict[str, ParamOwner] = dict.fromkeys(spec.require_backend().params, "kb")
    for key in derived_params(spec):
        owners[key] = "derived"
    for key in processing_params(processing.processing.quantification.value):
        owners[key] = "processing"
    return owners


def param_block_key(spec: Spec) -> str:
    """Which config block carries this spec's aligner params: ``solo`` xor ``bulk``.

    Keyed by the MODULE, which is the only thing that decides it. The gate used to instead take
    "whichever of the two happens to be a dict", so a bulk config carrying a stray ``solo`` block was
    reported as *"config drops KB param 'quantMode'"* — a real failure diagnosed as an unrelated one,
    which is worse than no gate: it sends you to the wrong file. One definition, consulted by both the
    composer that writes the block and the gate that checks it.

    And the module reads it off its own source. This function used to be
    ``"solo" if spec.backend.module == "map/starsolo" else "bulk"`` — the last string compare against
    a module name in the tree, and the same shape as the `_read_files_in` bug that preceded it: every
    module that is not starsolo silently means bulk. See :attr:`WorkflowModule.param_block`.
    """
    return str(get_module(spec.require_backend().module).param_block)


def render_param(value: object) -> str:
    """Render a KB backend param the way a CLI takes it (a list becomes space-separated)."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def _resolves_to_onlist_path(value: object) -> bool:
    """A KB param whose value is an ``{onlist:<alias>}`` token, or a list of them.

    Such a value is resolved to a materialized whitelist PATH at compose time (see
    ``compose.core._resolve_token``), so its config rendering is a path, not the verbatim token — the
    per-key faithfulness check must skip it or it would compare a path against a token and always fail.
    Both STARsolo's ``soloCBwhitelist`` and chromap's ``barcode_whitelist`` are such params; keying on
    the VALUE rather than the key name covers a third one without spelling it out.
    """
    values = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and v.startswith("{onlist:") for v in values)


def _as_int(value: object) -> int | None:
    """KB params arrive as int or str depending on the YAML; compare them numerically."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _element(read: ReadDef, role: str) -> ReadElement | None:
    for el in read.elements:
        if el.role == role:
            return el
    return None


def find_read_with_role(manifest: DatasetManifest, role: str) -> ReadDef | None:
    """The layout read carrying an element of ``role`` (e.g. the cDNA read, the CB-bearing read)."""
    for read in manifest.library.read_layout.reads:
        if any(el.role == role for el in read.elements):
            return read
    return None


def params_gate(
    manifest: DatasetManifest,
    processing: ProcessingManifest,
    spec: Spec,
    config: dict[str, object],
) -> tuple[GateStatus, list[str]]:
    """Assert every emitted param is owned, arrives verbatim, and agrees with the observed layout."""
    problems: list[str] = []
    backend = spec.require_backend()
    params = backend.params
    from_processing = processing_params(processing.processing.quantification.value)
    from_derived = derived_params(spec)

    # ---- 1. disjointness: one key, one owner ----
    both = sorted(set(params) & RECIPE_PARAM_KEYS)
    if both:
        problems.append(
            f"KB declares count key(s) {both}, which the processing manifest owns: backend.params "
            f"says how to PARSE reads, not what to COUNT"
        )
    stray = sorted(set(params) - parse_keys_for(backend.module))
    if stray:
        problems.append(f"KB declares non-parse key(s) {stray}")
    if spec.read_through and "read_through" not in from_derived:
        # Not "the pipeline forgot a key": the pipeline never offered to clip, and the entry said it
        # would be clipped. Left standing, the adapter stays inside STAR's length-relative filter and
        # is counted against a read it is no part of, so every cell loses the same fraction it lost
        # before while the chemistry now reads as handled. A wrong number at exit 0, refused here.
        problems.append(
            f"KB declares read_through={spec.read_through!r}, which pipeline {backend.module!r} "
            f"cannot clip. A declared adapter nothing removes is worse than none: it still costs "
            f"reads to the aligner's length-relative filter, and now nothing says so"
        )
    redeclared = sorted(set(params) & DERIVED_PARAM_KEYS)
    if redeclared:
        problems.append(
            f"KB declares derived key(s) {redeclared}: these are computed from the element "
            f"coordinates, which already state them. Declaring them here is the same fact twice, "
            f"and the two copies can drift"
        )

    block = param_block_key(spec)
    found = config.get(block)
    if not isinstance(found, dict):
        # ONE root cause, not N derivative ones. Enumerating every key as "dropped" on top of this
        # buries the actual fault under a list that points at the KB, which is the one file that is
        # fine. A gate is read by someone who does not yet know what is wrong.
        problems.append(f"config has no {block!r} param block (module is {backend.module!r})")
    else:
        emitted: dict[str, object] = found
        # ---- 2. coverage: the emitted key set is EXACTLY the union of the three owners ----
        expected_keys = set(params) | set(from_processing) | set(from_derived)
        orphans = sorted(set(emitted) - expected_keys)
        if orphans:
            problems.append(f"config emits param(s) {orphans} that no owner declares")
        missing = sorted(expected_keys - set(emitted))
        if missing:
            problems.append(f"config drops declared param(s) {missing}")

        # ---- 3. faithfulness, per key, per owner ----
        for key, expected in params.items():
            if _resolves_to_onlist_path(expected):
                continue  # an {onlist:...} token is resolved to a path at compose, so it is not
                # compared verbatim (the registry proves the whitelist exists in `_resolve_token`).
                # Value-based, not a key name: covers STARsolo's `soloCBwhitelist` AND chromap's
                # `barcode_whitelist` without either being spelled out here.
            want = render_param(expected)
            got = emitted.get(key)
            if got is not None and str(got) != want:
                problems.append(f"config {key}={got!r} does not match KB {key}={want!r}")
        for key, expected_p in from_processing.items():
            want = render_param(expected_p)
            got = emitted.get(key)
            if got is not None and str(got) != want:
                problems.append(
                    f"config {key}={got!r} does not match the processing manifest's "
                    f"{key}={want!r} — quantification must not be decorative"
                )
        for key, expected_d in from_derived.items():
            got = emitted.get(key)
            if got is not None and str(got) != expected_d:
                problems.append(
                    f"config {key}={got!r} does not match {key}={expected_d!r} derived from the "
                    f"element coordinates — the spec's elements are the only source for this"
                )

    # ---- 4. cross-derivation: KB offsets/lengths must agree with the OBSERVED read layout ----
    if params.get("soloType") == "CB_UMI_Simple":
        bc_read = find_read_with_role(manifest, "CB")
        if bc_read is None:
            problems.append("layout has no CB-bearing read, but soloType is CB_UMI_Simple")
        else:
            problems += _check_simple_geometry(bc_read, params)
    if "read_structure" in from_derived:
        problems += _check_read_structure(manifest, from_derived["read_structure"])

    # ---- 5. pairwise legality: (soloType, soloCBmatchWLtype) must be a pair STAR accepts ----
    problems += _check_cb_match_wl_type(params)

    # ---- 6. readFilesIn: each role maps to the byte-decided read (per this pipeline's layout kind) ----
    problems += _check_read_files_in(manifest, config, get_module(backend.module).read_layout_kind)

    return ("fail" if problems else "pass"), problems


def _check_cb_match_wl_type(params: Mapping[str, object]) -> list[str]:
    """Refuse a ``(soloType, soloCBmatchWLtype)`` pair STAR would FATAL on — see :data:`CB_MATCH_WL_TYPES`.

    Gated on ``soloType`` being declared at all, which is what scopes this to STARsolo: a bulk or a
    chromap backend has no such key and falls straight through, rather than being asked about a flag
    its aligner has never heard of.

    A **missing** ``soloCBmatchWLtype`` is refused too, and that is the same bug wearing different
    clothes. The module dereferences the key unconditionally, so a starsolo spec that omits it dies at
    Snakemake parse time on a compute node; and if the module instead fell back to STAR's default, a
    Complex spec would die at STAR on a value it never chose. Both are "a run that ends on the node",
    which is what this gate exists to convert into an exit code.
    """
    solo_type = params.get("soloType")
    if solo_type is None:
        return []
    legal = CB_MATCH_WL_TYPES.get(str(solo_type))
    if legal is None:
        return [
            f"KB declares soloType={solo_type!r}, whose legal --soloCBmatchWLtype values this gate "
            f"does not know (it knows {sorted(CB_MATCH_WL_TYPES)}). Add the measured row to "
            f"CB_MATCH_WL_TYPES rather than letting an unchecked pair reach a compute node"
        ]
    value = params.get("soloCBmatchWLtype")
    if value is None:
        return [
            f"KB declares soloType={solo_type!r} but no soloCBmatchWLtype; every starsolo chemistry "
            f"must name its own barcode-match mode (legal here: {sorted(legal)}). STAR's global "
            f"default is 1MM_multi, which CB_UMI_Complex rejects outright, so there is no safe value "
            f"to fall back to"
        ]
    if str(value) not in legal:
        return [
            f"KB soloCBmatchWLtype={str(value)!r} is illegal for soloType={solo_type!r}; STAR accepts "
            f"{sorted(legal)} there. This is a hard STAR FATAL raised after the genome loads, so "
            f"compose refuses it now instead of a compute node refusing it in twenty minutes"
        ]
    return []


def _check_read_structure(manifest: DatasetManifest, emitted: str) -> list[str]:
    """The geometry derived from the KB must equal the one derived from the OBSERVED layout.

    The plate module's cross-derivation, and the same claim ``_check_simple_geometry`` makes for a
    simple STARsolo chemistry: a KB whose element coordinates contradict the reads is a run that cuts
    a UMI out of the wrong bases. It is worth stating separately here because this pipeline's entire
    config block is one derived value — there is no declared offset for the KB to get wrong, only
    the coordinates themselves, so this is the only place the two element models are made to agree.
    """
    from ..workflows.umite.extract import UmiExtractError, tagged_read_geometry

    try:
        observed = tagged_read_geometry(manifest.library.read_layout).render()
    except UmiExtractError as exc:
        return [
            f"the observed read layout carries no extractable tagged read ({exc}), but the "
            f"chemistry's elements derive read_structure={emitted!r}"
        ]
    if observed != emitted:
        return [
            f"read_structure={emitted!r} derived from the chemistry's elements contradicts "
            f"{observed!r} derived from the observed read layout — the two element models state one "
            f"geometry and this dataset's bytes were decided to be a different one"
        ]
    return []


def _check_simple_geometry(bc_read: ReadDef, params: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    cb = _element(bc_read, "CB")
    umi = _element(bc_read, "UMI")
    # lengths: the KB's declared width must equal the width actually present in the reads
    lengths = [
        ("soloCBlen", cb.length if cb else None, "CB length"),
        ("soloUMIlen", umi.length if umi else None, "UMI length"),
    ]
    for key, observed, label in lengths:
        want = _as_int(params.get(key))
        if want is not None and observed is not None and want != observed:
            problems.append(f"KB {key}={want} contradicts the observed {label} of {observed} bp")
    # starts: STARsolo offsets are 1-based; the element model is 0-based half-open.
    starts = [
        ("soloCBstart", cb.start if cb else None, "CB"),
        ("soloUMIstart", umi.start if umi else None, "UMI"),
    ]
    for key, start0, label in starts:
        want = _as_int(params.get(key))
        if want is not None and start0 is not None and want != start0 + 1:
            problems.append(
                f"KB {key}={want} (1-based) contradicts the observed {label} start "
                f"{start0} (0-based) -> expected {start0 + 1}"
            )
    return problems


def _check_read_files_in(
    manifest: DatasetManifest, config: Mapping[str, object], layout_kind: ReadLayoutKind
) -> list[str]:
    """Assert config's read->role map matches the byte-decided layout, per this pipeline's layout kind.

    Dispatch is on the PIPELINE's ``read_layout_kind``, the same axis the composer's ``_read_files_in``
    dispatches on — so the gate checks exactly the mapping the composer was supposed to emit, rather than
    inferring the shape from ``soloType`` (which a non-STARsolo pipeline like chromap does not carry, and
    which would then have silently fallen into the bulk mate1/mate2 branch).

    Every kind is named and none is the ``else``, for that same reason one level down: an unhandled
    kind reaching a gate's fall-through is a gate agreeing with the composer because both guessed the
    same way. ``assert_never`` makes it a type error instead.
    """
    problems: list[str] = []
    rfi = config.get("read_files_in")
    if not isinstance(rfi, dict):
        return ["config has no read_files_in mapping"]
    if layout_kind == "barcoded":
        cdna_read = find_read_with_role(manifest, "cDNA") or find_read_with_role(manifest, "gDNA")
        bc_read = find_read_with_role(manifest, "CB")
        if cdna_read is None or bc_read is None:
            problems.append("a barcoded chemistry needs both a cDNA read and a CB-bearing read")
            return problems
        if rfi.get("cdna") != cdna_read.read_id:
            problems.append(
                f"read_files_in.cdna={rfi.get('cdna')!r} is not the cDNA read {cdna_read.read_id!r}"
            )
        if rfi.get("barcode") != bc_read.read_id:
            problems.append(
                f"read_files_in.barcode={rfi.get('barcode')!r} is not the CB read {bc_read.read_id!r}"
            )
        if rfi.get("cdna") == rfi.get("barcode"):
            problems.append("read_files_in maps the cDNA and barcode roles to the same read")
    elif layout_kind == "atac_barcoded":
        gdna = [
            r
            for r in manifest.library.read_layout.reads
            if any(el.role == "gDNA" for el in r.elements)
        ]
        bc_read = find_read_with_role(manifest, "CB")
        if len(gdna) < 2 or bc_read is None:
            problems.append("scATAC needs two genomic (gDNA) reads and a barcode read")
            return problems
        expected = {"gdna1": gdna[0].read_id, "gdna2": gdna[1].read_id, "barcode": bc_read.read_id}
        for key, want in expected.items():
            if rfi.get(key) != want:
                problems.append(
                    f"read_files_in.{key}={rfi.get(key)!r} is not the {key} read {want!r}"
                )
        if len({rfi.get("gdna1"), rfi.get("gdna2"), rfi.get("barcode")}) != 3:
            problems.append("read_files_in maps two scATAC roles to the same read")
    elif layout_kind == "umi_tagged":
        # ROLE, not order, and the assertion is the load-bearing one for this pipeline: the two mates
        # of a plate assay are not symmetric, so handing the extractor the plain one yields a uBAM
        # with no UMI anywhere, an empty matrix, and exit 0 all the way down.
        tagged = find_read_with_role(manifest, "UMI")
        if tagged is None:
            problems.append("a umi_tagged chemistry needs a read carrying a UMI element")
            return problems
        if rfi.get("umi_cdna") != tagged.read_id:
            problems.append(
                f"read_files_in.umi_cdna={rfi.get('umi_cdna')!r} is not the tagged read "
                f"{tagged.read_id!r}; the extractor would find no tags at all and exit 0"
            )
        mate = rfi.get("cdna")
        if mate is not None and mate == rfi.get("umi_cdna"):
            problems.append("read_files_in maps the tagged read and its mate to the same read")
        others = [
            r.read_id for r in manifest.library.read_layout.reads if r.read_id != tagged.read_id
        ]
        if mate is not None and mate not in others:
            problems.append(f"read_files_in.cdna={mate!r} is not a read this layout carries")
    elif layout_kind == "mates":
        # 1..2 biological mates chosen by ORDER, with no barcode role to name one by. The whole
        # mapping is re-derived and compared, rather than the mates being checked for membership and
        # distinctness as they were while every mates layout had exactly two reads. Membership no
        # longer says enough: it cannot tell a `mate2` the layout does not have from one it does, and
        # "the config emitted a key for a mate that is not there" is the failure this kind's widening
        # introduces. Same shape as the two branches above, which have always compared a derivation.
        reads = manifest.library.read_layout.reads
        if not reads:
            problems.append("a mates layout needs at least one read, and this one has none")
            return problems
        expected = {f"mate{n}": r.read_id for n, r in enumerate(reads[:2], start=1)}
        emitted = {k: v for k, v in rfi.items() if k.startswith("mate")}
        if emitted != expected:
            problems.append(
                f"read_files_in mates {emitted} are not the layout's mates {expected}, in order"
            )
    else:
        assert_never(layout_kind)
    return problems
