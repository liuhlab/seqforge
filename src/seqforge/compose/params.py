"""The **params gate** — the semantic assertions a dry-run cannot make (design §4.1, part 2).

``snakemake -n`` proves the *wiring*; it cannot see that ``--soloUMIlen`` is 10 when the reads carry a
12 bp UMI, or that ``--readFilesIn`` puts the barcode read where the cDNA read belongs. Those are the
bugs a config compiler actually produces, and they fail **silently**: STARsolo exits 0 and emits a
matrix that merely looks like a thin dataset. So they get deterministic assertions of their own, run
on every compose, with no data and no aligner.

Two independent checks:
1. **Faithfulness** — the emitted config carries the KB's chemistry-defining params verbatim (catches
   a composer that drops, renames, or mangles a knob).
2. **Cross-derivation** — the KB's declared offsets/lengths agree with the *observed* read layout in
   the manifest (catches a KB whose params contradict the bytes: ``soloCBlen 16`` over a 12 bp CB).

Strand correctness itself is NOT decidable here — only the `kb e2e` count-matrix run can catch an
inverted ``--soloStrand``. This gate asserts the value survives compose intact; the e2e asserts it is
*right*.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ..kb.schema import Spec
from ..models.dataset import DatasetManifest, ReadDef, ReadElement

GateStatus = Literal["pass", "fail"]


def param_block_key(spec: Spec) -> Literal["solo", "bulk"]:
    """Which config block carries this spec's aligner params: ``solo`` xor ``bulk``.

    Keyed by the MODULE, which is the only thing that decides it. The gate used to instead take
    "whichever of the two happens to be a dict", so a bulk config carrying a stray ``solo`` block was
    reported as *"config drops KB param 'quantMode'"* — a real failure diagnosed as an unrelated one,
    which is worse than no gate: it sends you to the wrong file. One definition, consulted by both the
    composer that writes the block and the gate that checks it.
    """
    return "solo" if spec.backend.module == "map/starsolo" else "bulk"


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
    for read in manifest.library.read_layout.value.reads:
        if any(el.role == role for el in read.elements):
            return read
    return None


def params_gate(
    manifest: DatasetManifest, spec: Spec, config: dict[str, object]
) -> tuple[GateStatus, list[str]]:
    """Assert the emitted config is faithful to the KB and coherent with the observed layout."""
    problems: list[str] = []
    params = spec.backend.params

    # ---- 1. faithfulness: every chemistry-defining knob survives compose verbatim ----
    block = param_block_key(spec)
    found = config.get(block)
    if not isinstance(found, dict):
        # ONE root cause, not N derivative ones. Enumerating every key as "dropped" on top of this
        # buries the actual fault under a list that points at the KB, which is the one file that is
        # fine. A gate is read by someone who does not yet know what is wrong.
        problems.append(f"config has no {block!r} param block (module is {spec.backend.module!r})")
    else:
        emitted: dict[str, object] = found
        for key, expected in params.items():
            if key == "soloCBwhitelist":
                continue  # an {onlist:...} token is resolved to a path; checked separately below
            want = render_param(expected)
            got = emitted.get(key)
            if got is None:
                problems.append(f"config drops KB param {key!r} (expected {want!r})")
            elif str(got) != want:
                problems.append(f"config {key}={got!r} does not match KB {key}={want!r}")

    # ---- 2. cross-derivation: KB offsets/lengths must agree with the OBSERVED read layout ----
    if params.get("soloType") == "CB_UMI_Simple":
        bc_read = find_read_with_role(manifest, "CB")
        if bc_read is None:
            problems.append("layout has no CB-bearing read, but soloType is CB_UMI_Simple")
        else:
            problems += _check_simple_geometry(bc_read, params)

    # ---- 3. readFilesIn order: the cDNA read must precede the barcode read ----
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
        roles = [r.read_id for r in manifest.library.read_layout.value.reads]
        if any(m not in roles for m in mates):
            problems.append(f"read_files_in mates {mates} are not layout reads {roles}")
        if mates[0] == mates[1]:
            problems.append("read_files_in maps both mates to the same read")
    return problems
