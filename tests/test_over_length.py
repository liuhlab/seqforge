"""Over-length barcode reads: an over-sequenced 10x R1 (CB/UMI in bp0-28, the rest junk) still
resolves to a concrete chemistry, and when length can no longer separate v2 from v3 the WHITELIST does.

The real GSE229022 has samples whose barcode read is sequenced to 150 bp. Length alone cannot tell a
150 bp v2 read from a 150 bp v3 read (both are "over-length") — so these tests prove the rung-3
whitelist decides, and decides *correctly* (the read whose first 16 bp hit a chemistry's onlist is
that chemistry's), and that an over-length read raises neither a blocker nor a spurious length conflict.
"""

from __future__ import annotations

import gzip
import random
from pathlib import Path

from seqforge import kb
from seqforge.io import OnlistRegistry
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset
from seqforge.resolve.engine import Hypothesis
from seqforge.resolve.scoring import build_tech_evaluation
from seqforge.resolve.window import WindowProbe

OVER_LEN = 150  # the run read length: an over-sequenced barcode read is this long, not 26/28


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@SIM:{i}\n{s}\n+\n{'I' * len(s)}\n")


def _registry_for(spec: kb.Spec) -> OnlistRegistry:
    pools = kb.build_pools(spec, seed=0)
    reg = OnlistRegistry(offline=True)
    for alias, ref in spec.onlists.items():
        if alias in pools:
            reg.register_synthetic(ref.registry, pools[alias])
    return reg


def _over_length(
    tmp_path: Path, tech: str, umi_len: int, total_len: int = OVER_LEN
) -> tuple[list[Path], dict[str, list[str]]]:
    """A barcode read (16 bp CB from the tech's whitelist + UMI + junk) and a cDNA read, both
    ``total_len`` bp. Default 150 bp (>= over_length_min, admitted on geometry); pass a dead-zone
    length (e.g. 75 bp) to exercise the onlist admission (#7).

    The CB is drawn from the tech's own pool so it hits that chemistry's whitelist and no other.
    """
    spec = kb.load_spec(tech)
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(0)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    seqs = {
        "R1": [
            rng.choice(cb_pool) + rand(umi_len) + rand(total_len - 16 - umi_len) for _ in range(600)
        ],
        "R2": [rand(total_len) for _ in range(600)],
    }
    paths = []
    for rid in ("R1", "R2"):
        p = tmp_path / f"{tech}_{rid}.fastq.gz"
        _write_fastq_gz(p, seqs[rid])
        paths.append(p)
    return paths, seqs


def test_an_over_length_v3_barcode_read_resolves_to_v3_via_its_whitelist(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    paths, _ = _over_length(tmp_path, "10x-3p-gex-v3", umi_len=12)
    reg = _registry_for(spec)  # registers ONLY the 3M-february-2018 (v3) whitelist

    out = resolve_dataset(paths, registry=reg, use_cache=False)
    assert not out.result.blockers, [b.message for b in out.result.blockers]
    winner = out.result.candidates[0]
    # v3 and v3.1 are §12 twins recorded together; either is the right answer, v2 is not.
    assert winner.technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}
    assert winner.score.status == "scored"
    # Both 150 bp reads were assigned (the barcode read to R1, cDNA to R2) — nothing dropped despite
    # the over-length: the whole point is that an over-sequenced R1 is not left unassigned.
    assert set(winner.role_assignment.assignment) == {"R1", "R2"}
    assert not winner.role_assignment.unassigned
    # The chemistry was decided by the onlist (rung 3): length could not do it.
    assert winner.rung_resolved.get("chemistry", 0) >= 3


def test_an_over_length_v2_barcode_read_resolves_to_v2_not_v3(tmp_path: Path) -> None:
    """Same 150 bp geometry, but the CB hits 737K-august-2016 -> v2. The whitelist alone separates."""
    spec = kb.load_spec("10x-3p-gex-v2")
    paths, _ = _over_length(tmp_path, "10x-3p-gex-v2", umi_len=10)
    reg = _registry_for(spec)  # registers ONLY the 737K-august-2016 (v2) whitelist

    out = resolve_dataset(paths, registry=reg, use_cache=False)
    assert not out.result.blockers, [b.message for b in out.result.blockers]
    winner = out.result.candidates[0]
    assert winner.technology == "10x-3p-gex-v2"
    assert winner.score.status == "scored"


DEAD_LEN = 75  # in the over-length DEAD ZONE: > canonical 26/28 bp, < over_length_min (100)


def test_a_dead_zone_barcode_read_is_admitted_by_its_whitelist(tmp_path: Path) -> None:
    """#7: an R1 over-sequenced to 75 bp sits in the DEAD ZONE — too long to be the canonical 26 bp v2
    read, too short for the over_length_min (100) that admits a full-length over-sequenced read. Length
    alone forbids it, and that is deliberate (a 60-94 bp cDNA must not pass as a barcode). The WHITELIST
    admits it: the first 16 bp hit 737K-august-2016, so this IS a real v2 barcode read. GSE126954's
    over-sequenced SRX5411291 is exactly this, and before the fix it collapsed to bulk-rnaseq-pe.
    """
    spec = kb.load_spec("10x-3p-gex-v2")
    paths, _ = _over_length(tmp_path, "10x-3p-gex-v2", umi_len=10, total_len=DEAD_LEN)
    reg = _registry_for(spec)  # registers ONLY the 737K-august-2016 (v2) whitelist

    out = resolve_dataset(paths, registry=reg, use_cache=False)
    assert not out.result.blockers, [b.message for b in out.result.blockers]
    winner = out.result.candidates[0]
    assert winner.technology == "10x-3p-gex-v2", [c.technology for c in out.result.candidates[:3]]
    # admitted BY the onlist, so the chemistry is decided at rung 3 (the over-length length gate FAILed)
    assert winner.rung_resolved.get("chemistry", 0) >= 3
    assert set(winner.role_assignment.assignment) == {"R1", "R2"}
    assert not winner.role_assignment.unassigned


def test_a_dead_zone_barcode_read_below_the_support_gate_is_still_admitted(tmp_path: Path) -> None:
    """The REAL SRX5411291 case the perfect-whitelist fixtures never exposed. Those draw every CB from
    the pool, so the exact hit rate is ~1.0 — above BOTH the 0.6 support gate and the admission bar, so
    they pass whichever bar admission uses. Real over-sequenced barcode reads carry ordinary sequencing
    error; seqforge matches CBs EXACTLY (no 1MM correction, which STARsolo does), so the exact hit rate
    sits well below 0.6. Here ~60% of CBs carry a 1 bp error -> exact hit ~0.4: below the support gate,
    far above the whitelist floor. The support-`min` gate rejected it and the sample collapsed to bulk;
    the floor-anchored admission bar (barcode-vs-cDNA) admits it. This test FAILS under the old gate.
    """
    spec = kb.load_spec("10x-3p-gex-v2")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(0)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    def one_error(cb: str) -> str:
        """Flip one base -> misses the EXACT-match whitelist (a 1 bp mismatch STARsolo would correct).
        The chance the flip lands on another whitelist entry is ~n_entries/4^16 ≈ 2e-4, negligible."""
        i = rng.randrange(16)
        return cb[:i] + rng.choice([b for b in "ACGT" if b != cb[i]]) + cb[i + 1 :]

    barcode = []
    for i in range(600):
        cb = rng.choice(cb_pool)
        if i % 5 >= 2:  # ~60% carry a 1 bp error -> exact hit rate ~0.4 (0.05 < 0.4 < 0.6)
            cb = one_error(cb)
        barcode.append(cb + rand(10) + rand(DEAD_LEN - 26))
    cdna = [rand(DEAD_LEN) for _ in range(600)]
    r1 = tmp_path / "v2_R1.fastq.gz"
    r2 = tmp_path / "v2_R2.fastq.gz"
    _write_fastq_gz(r1, barcode)
    _write_fastq_gz(r2, cdna)
    reg = _registry_for(spec)  # ONLY the 737K-august-2016 (v2) whitelist

    # Half one (the admission calibration): the v2 barcode role is admitted (not forbidden) at a
    # sub-0.6 hit rate. Fails under the old support-`min` gate.
    probes = [
        WindowProbe(observation=probe_file(p), seqs=s) for p, s in ((r1, barcode), (r2, cdna))
    ]
    ev = build_tech_evaluation(spec, probes, reg)
    assert ev.valid, (
        "a dead-zone barcode read hitting the whitelist below 0.6 must still be admitted"
    )

    # Half two (the dominance rule): end to end it resolves to v2 (rung 3), not bulk. At this hit rate
    # bulk has the higher RAW score (a 75 bp read is a fine cDNA), so admission alone is not enough --
    # the barcoded rung-3 candidate must not be shadowed by the barcodeless fallback (escalate anchor).
    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    assert winner.technology == "10x-3p-gex-v2", [c.technology for c in out.result.candidates[:3]]
    assert winner.rung_resolved.get("chemistry", 0) >= 3


def test_a_dead_zone_read_that_misses_every_whitelist_is_not_admitted(tmp_path: Path) -> None:
    """The safety half, and why the admission is keyed on the whitelist and not on length: a 75 bp read
    whose first 16 bp hit NO whitelist is a cDNA/junk read, not a barcode. The admission must NOT fire —
    the read stays forbidden for v2 and the data resolves to the generic bulk fallback. If this ever
    regressed, any 60-94 bp cDNA would be admitted as a barcode read and rungs 0-2 would stop being
    separable."""
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "x_R1.fastq.gz"
    r2 = tmp_path / "x_R2.fastq.gz"
    _write_fastq_gz(r1, [rand(DEAD_LEN) for _ in range(600)])  # random 75 bp -> hits no whitelist
    _write_fastq_gz(r2, [rand(DEAD_LEN) for _ in range(600)])
    reg = _registry_for(
        kb.load_spec("10x-3p-gex-v2")
    )  # v2 whitelist IS registered; the reads miss it

    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    winner = out.result.candidates[0] if out.result.candidates else None
    assert winner is not None
    assert winner.technology != "10x-3p-gex-v2", (
        "a whitelist-missing 75 bp read must not be admitted"
    )
    assert winner.technology == "bulk-rnaseq-pe"


def test_genuine_bulk_still_resolves_to_bulk_with_barcode_whitelists_registered(
    tmp_path: Path,
) -> None:
    """Safety guard for the dominance anchor (a barcoded rung-3 candidate is not shadowed by the
    barcodeless fallback): it must NEVER hijack genuine bulk. Canonical ~100 bp paired cDNA reads with
    NO barcode content, resolved with the v2 whitelist registered, must still resolve to bulk-rnaseq-pe
    — no barcoded chemistry reaches rung 3 (the reads miss the whitelist), so there is nothing to
    promote and the anchor stays on bulk. This is the invariant that keeps every real bulk dataset
    (and any single-cell dataset whose barcode read is genuinely absent) unaffected."""
    rng = random.Random(7)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    r1 = tmp_path / "bulk_R1.fastq.gz"
    r2 = tmp_path / "bulk_R2.fastq.gz"
    _write_fastq_gz(r1, [rand(100) for _ in range(600)])  # canonical cDNA length, no barcode
    _write_fastq_gz(r2, [rand(100) for _ in range(600)])
    reg = _registry_for(kb.load_spec("10x-3p-gex-v2"))  # whitelist registered but never hit

    out = resolve_dataset([r1, r2], registry=reg, use_cache=False)
    assert out.result.candidates[0].technology == "bulk-rnaseq-pe", [
        c.technology for c in out.result.candidates[:3]
    ]


def test_both_v2_and_v3_accept_the_over_length_read_on_geometry_alone(tmp_path: Path) -> None:
    """Why the whitelist is load-bearing: at rungs 0-2 (onlist withheld) BOTH chemistries accept the
    150 bp read, so neither length nor segmentation can pick — exactly the sub-rung-3 tie the
    v2<->v3 confusable_with declaration is honest about."""
    paths, seqs = _over_length(tmp_path, "10x-3p-gex-v3", umi_len=12)
    empty = OnlistRegistry(offline=True)  # withhold every whitelist -> rungs 0-2 only
    probes = [
        WindowProbe(observation=probe_file(p), seqs=seqs[rid])
        for p, rid in zip(paths, ("R1", "R2"), strict=True)
    ]
    for tech in ("10x-3p-gex-v2", "10x-3p-gex-v3"):
        ev = build_tech_evaluation(kb.load_spec(tech), probes, empty)
        assert ev.valid, f"{tech} should accept the over-length read on geometry alone"


def test_an_over_length_read_with_a_ragged_tail_is_not_flagged_as_pretrimmed(
    tmp_path: Path,
) -> None:
    """A trimmed barcode read blocks (its offsets shifted); an over-length read whose *junk tail* is
    ragged does not — CB/UMI are intact at the fixed offsets, so that variation is harmless."""
    spec = kb.load_spec("10x-3p-gex-v3")
    cb_pool = kb.build_pools(spec, seed=0)["cb_whitelist"]
    rng = random.Random(1)

    def rand(n: int) -> str:
        return "".join(rng.choice("ACGT") for _ in range(n))

    # mode 150 (over-length), but a minority of reads have a shorter junk tail -> n_distinct > 1.
    barcode = [rng.choice(cb_pool) + rand(12) + rand(122 if i % 10 else 100) for i in range(600)]
    cdna = [rand(OVER_LEN) for _ in range(600)]
    r1 = tmp_path / "v3_R1.fastq.gz"
    r2 = tmp_path / "v3_R2.fastq.gz"
    _write_fastq_gz(r1, barcode)
    _write_fastq_gz(r2, cdna)

    out = resolve_dataset([r1, r2], registry=_registry_for(spec), use_cache=False)
    assert not any(b.code.name == "PRETRIMMED_VARIABLE_LENGTH" for b in out.result.blockers), [
        b.message for b in out.result.blockers
    ]
    assert out.result.candidates[0].technology in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}


def test_no_spurious_barcode_length_conflict_for_an_over_length_read(tmp_path: Path) -> None:
    """A v3 hypothesis on a 150 bp barcode read must NOT raise `conflict-barcode-length` (28 vs 150):
    the over-length read is expected geometry, not a contradiction to surface."""
    spec = kb.load_spec("10x-3p-gex-v3")
    paths, _ = _over_length(tmp_path, "10x-3p-gex-v3", umi_len=12)
    reg = _registry_for(spec)

    out = resolve_dataset(
        paths, registry=reg, hypothesis=Hypothesis(value="10x-3p-gex-v3"), use_cache=False
    )
    assert not any(c.id == "conflict-barcode-length" for c in out.result.conflicts), [
        c.id for c in out.result.conflicts
    ]
