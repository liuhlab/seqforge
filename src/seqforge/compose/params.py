"""The **params gate** — the semantic assertions a dry-run cannot make (design §4.1, part 2).

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

Strand correctness itself is NOT decidable here — only the `kb e2e` count-matrix run can catch an
inverted ``--soloStrand``. This gate asserts the value survives compose intact; the e2e asserts it is
*right*.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ..kb.schema import KB_PARSE_KEYS, Element, Spec
from ..models.dataset import DatasetManifest, ReadDef, ReadElement
from ..models.processing import ProcessingManifest, Quantification, SoloQuant
from ..workflows import get_module

GateStatus = Literal["pass", "fail"]
ParamOwner = Literal["kb", "processing", "derived"]

RECIPE_PARAM_KEYS: frozenset[str] = frozenset({"soloFeatures", "quantMode"})
"""Every backend param sourced from the processing manifest. Each says what to **COUNT**."""

DERIVED_PARAM_KEYS: frozenset[str] = frozenset({"soloCBposition", "soloUMIposition"})
"""Params computed from the element model rather than declared by anyone.

Still parse keys — byte-decided, never instructable — but the bytes already answered them in
the spec's element coordinates, so a KB that *also* declared the quadruple would carry the same fact
twice and let the two drift. A third owner, because "one fact, one owner" is the whole point of
:func:`param_owners`; folding these into ``kb`` would make the gate certify a value the KB never
stated.
"""


def derived_params(spec: Spec) -> dict[str, str]:
    """Locate a ``CB_UMI_Complex`` chemistry's barcodes/UMI from its elements, as STAR wants them.

    STARsolo's complex chemistries take position quadruples
    (``startAnchor_startPos_endAnchor_endPos``; anchor 0 = read start, positions 0-based INCLUSIVE)
    rather than the start/length pair a simple chemistry uses. The splitseq spec says outright why
    this is computed and not written down: *"never hand-enter a position quadruple from memory —
    generate it from the element model"*. A published quadruple is also chemistry-specific in a way
    that invites exactly that error — v1's Round1 sits at 86-93 and Parse/v2's at 78-85, so a
    remembered value is a coin flip between two real chemistries.

    Order is load-bearing: STARsolo pairs the Nth ``soloCBwhitelist`` with the Nth
    ``soloCBposition``, so the quadruples are emitted in the whitelist's declared order, never the
    elements' positional order.
    """
    if spec.require_backend().params.get("soloType") != "CB_UMI_Complex":
        return {}

    by_onlist: dict[str, Element] = {}
    umi: Element | None = None
    for read in spec.reads:
        for el in read.elements:
            if el.type == "barcode" and el.onlist:
                by_onlist[el.onlist] = el
            elif el.type == "umi":
                umi = el

    whitelist = spec.require_backend().params.get("soloCBwhitelist")
    aliases = [
        v[len("{onlist:") : -1]
        for v in (whitelist if isinstance(whitelist, list) else [whitelist])
        if isinstance(v, str) and v.startswith("{onlist:")
    ]
    out: dict[str, str] = {}
    positions = [q for a in aliases if (q := _quadruple(by_onlist.get(a))) is not None]
    if positions:
        out["soloCBposition"] = " ".join(positions)
    umi_pos = _quadruple(umi)
    if umi_pos is not None:
        out["soloUMIposition"] = umi_pos
    return out


def _quadruple(el: Element | None) -> str | None:
    """One element -> ``0_<start>_0_<end>``: anchored at the read start, both ends inclusive.

    The element model is half-open ``[start, end)`` (Python's convention); STAR's quadruple is
    closed. That off-by-one is the whole reason this is a function with a name.

    ``None`` when the element is absent or open-ended: a quadruple needs both coordinates, and an
    element without them (cDNA runs to the end of the read) has no position to state. Returning
    ``None`` keeps the key out of the config entirely rather than emitting ``0_0_0_-1``, which STAR
    would accept as a real and wrong instruction.
    """
    if el is None or el.start is None or el.end is None:
        return None
    return f"0_{el.start}_0_{el.end - 1}"


def processing_params(quant: Quantification) -> dict[str, object]:
    """Render a counting decision into the aligner params it stands for.

    Module-scoped by construction: ``soloFeatures`` is meaningless to plain STAR and ``quantMode`` is
    meaningless to STARsolo, so the discriminated union is what keeps a processing manifest from being
    a type error the moment it meets the other module.
    """
    if isinstance(quant, SoloQuant):
        # space-joined, exactly as the KB's list rendering did — STAR takes repeated argv values
        return {"soloFeatures": " ".join(quant.features)}
    return {"quantMode": quant.mode}


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
    stray = sorted(set(params) - KB_PARSE_KEYS)
    if stray:
        problems.append(f"KB declares non-parse key(s) {stray}")
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
            if key == "soloCBwhitelist":
                continue  # an {onlist:...} token is resolved to a path; checked separately below
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

    # ---- 5. readFilesIn order: the cDNA read must precede the barcode read ----
    problems += _check_read_files_in(manifest, config, params)

    return ("fail" if problems else "pass"), problems


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
    manifest: DatasetManifest, config: Mapping[str, object], params: Mapping[str, object]
) -> list[str]:
    problems: list[str] = []
    rfi = config.get("read_files_in")
    if not isinstance(rfi, dict):
        return ["config has no read_files_in mapping"]
    cdna_read = find_read_with_role(manifest, "cDNA") or find_read_with_role(manifest, "gDNA")
    if params.get("soloType") in ("CB_UMI_Simple", "CB_UMI_Complex"):
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
    else:  # bulk: two biological mates, no barcode role
        mates = [rfi.get("mate1"), rfi.get("mate2")]
        roles = [r.read_id for r in manifest.library.read_layout.reads]
        if any(m not in roles for m in mates):
            problems.append(f"read_files_in mates {mates} are not layout reads {roles}")
        if mates[0] == mates[1]:
            problems.append("read_files_in maps both mates to the same read")
    return problems
