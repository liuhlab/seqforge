"""Technical sample-index reads (10x I1/I2): recognized, tagged ``index``, set aside from STARsolo.

STARsolo consumes only the CB+UMI read and the cDNA read. An 8/10 bp sample-index file is a leftover
of an *already-decided* run — it must be set aside, not left unassigned (which blocks a clean sample)
and not forced into the layout (which would demand an index file of *every* sample). The resolver
tags such a leftover ``index`` only when the bytes say it is index-sized; a longer stray leftover
stays unassigned and still blocks loudly. These tests drive the real probe -> resolve -> fill ->
validate -> compose path on synthetic reads, so the whole chain is exercised, not a hand-built model.
"""

from __future__ import annotations

import gzip
import random
from collections import Counter
from pathlib import Path

from seqforge import __version__, kb
from seqforge.compose import core
from seqforge.io import OnlistRegistry
from seqforge.manifest import (
    ExperimentInputs,
    exit_code_for_report,
    fill_manifest,
    validate_manifest,
)
from seqforge.models.blocker import BlockerCode
from seqforge.models.dataset import INDEX_ROLE, SampleGroup
from seqforge.models.evidenced import EvidencedTaxid
from seqforge.probe import probe_file
from seqforge.resolve import resolve_dataset, resolve_runs
from seqforge.resolve.engine import INDEX_MAX_LEN, index_tagged_roles

TECH = "10x-3p-gex-v3"


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


def _taxid(value: int) -> EvidencedTaxid:
    return EvidencedTaxid(value=value, basis="user_confirmed", rung=0)


def _reads(tmp_path: Path, *, extra: str | None) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """v3 R1(28)+R2 written as one run, optionally plus a third file.

    Files use ``fasterq-dump --include-technical``'s numeric mate suffixes (``_1`` / ``_2`` / ``_3``),
    which is how the real GSE229022 index reads arrive (``SRR..._1``, ``SRR..._2``) and what groups
    them into one run. ``extra="index"`` writes an 8 bp sample-index file; ``extra="cdna"`` writes a
    second cDNA-length file (a stray that must NOT be mistaken for an index read).
    """
    spec = kb.load_spec(TECH)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    paths: list[Path] = []
    for suffix, k in (("_1", "R1"), ("_2", "R2")):
        p = tmp_path / f"SRXidx{suffix}.fastq.gz"
        _write_fastq_gz(p, reads[k])
        paths.append(p)
    if extra == "index":
        p = tmp_path / "SRXidx_3.fastq.gz"
        _write_fastq_gz(p, [r[:8] for r in reads["R1"]])  # 8 bp, well under INDEX_MAX_LEN
        paths.append(p)
    elif extra == "cdna":
        p = tmp_path / "SRXidx_3.fastq.gz"
        _write_fastq_gz(p, list(reads["R2"]))  # cDNA-length; a real dropped read, not an index
        paths.append(p)
    return spec, reg, paths


#: The synthetic index/stray file is always the third-mate file of the one run.
_INDEX_BASENAME = "SRXidx_3.fastq.gz"


def _manifest(tmp_path: Path, spec: kb.Spec, reg: OnlistRegistry, paths: list[Path]):
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    return fill_manifest(
        result=out.result,
        spec=spec,
        observations=[probe_file(p) for p in paths],
        registry=reg,
        experiment=ExperimentInputs(
            organism=_taxid(6239),
            accessions=["PRJNA1027859"],
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in paths])],
        ),
        seqforge_version=__version__,
    )


# ------------------------------------------------------------ the length gate itself


def test_index_tagged_roles_tags_a_short_leftover_and_keeps_the_real_roles(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    roles = index_tagged_roles(winner, out.observations)

    index_sha = next(o.file.sha256 for o in out.observations if o.file.basename == _INDEX_BASENAME)
    assert roles[index_sha] == INDEX_ROLE
    # The CB and cDNA files keep the roles the optimizer gave them — the index tag is additive.
    assert set(roles.values()) >= {INDEX_ROLE}
    assert any(role != INDEX_ROLE for role in roles.values())


def test_a_cdna_length_leftover_is_never_tagged_index(tmp_path: Path) -> None:
    """The gate is a safety: a stray full-length read is a DROPPED read, not a technical index."""
    spec, reg, paths = _reads(tmp_path, extra="cdna")
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    winner = out.result.candidates[0]
    roles = index_tagged_roles(winner, out.observations)
    assert INDEX_ROLE not in roles.values()
    # Its length is above the gate, so it stays a leftover with no role at all (validate will block).
    assert len(roles) < len(paths)


def test_the_gate_sits_below_a_barcode_read_and_above_an_index(tmp_path: Path) -> None:
    # A documentation guard: 8/10 bp index reads pass, a 26 bp v2 / 28 bp v3 CB read never would.
    assert 10 < INDEX_MAX_LEN < 26


# ------------------------------------------------------------ multi-run engine path


def test_the_multirun_role_map_tags_the_index_read(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    role_of_sha = multi.role_of_sha()
    index_sha = next(
        o.file.sha256 for o in multi.observations if o.file.basename == _INDEX_BASENAME
    )
    assert role_of_sha[index_sha] == INDEX_ROLE
    assert not multi.blockers  # one chemistry, no disagreement


# ------------------------------------------------------------ validate + compose


def test_the_index_read_validates_clean_and_becomes_no_unit(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="index")
    manifest = _manifest(tmp_path, spec, reg, paths)

    # The index file is in the inventory, tagged, and the pipeline reads never include it.
    index_items = [f for f in manifest.library.files if f.read_id == INDEX_ROLE]
    assert len(index_items) == 1
    assert index_items[0].basename == _INDEX_BASENAME

    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    assert not any(r["path"].endswith("_3.fastq.gz") for r in rows)
    # The real reads still produce their units.
    assert {row["read_id"] for row in rows} == {f.read_id for f in manifest.library.files} - {
        INDEX_ROLE
    }


def test_a_stray_cdna_length_file_still_blocks(tmp_path: Path) -> None:
    spec, reg, paths = _reads(tmp_path, extra="cdna")
    manifest = _manifest(tmp_path, spec, reg, paths)

    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert not report.ok
    assert any(b.code == BlockerCode.NO_VALID_ROLE_ASSIGNMENT for b in report.blockers)


def test_a_clean_two_file_run_carries_no_index_role(tmp_path: Path) -> None:
    """The no-leftover case is byte-identical to before: nothing is ever tagged index."""
    spec, reg, paths = _reads(tmp_path, extra=None)
    manifest = _manifest(tmp_path, spec, reg, paths)
    assert not any(f.read_id == INDEX_ROLE for f in manifest.library.files)
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]


# ------------------------------------------------------------ multi-lane surplus absorption


def _multilane_reads(tmp_path: Path, lanes: int = 3) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession sequenced across ``lanes`` lanes: each lane an R1(28)+R2(90)+I1(8), named
    the bcl2fastq way (``SRR..._S1_L001_R1_001.fastq.gz``). The shared SRA accession groups every lane
    into ONE run -- the GSE208154 shape."""
    spec = kb.load_spec(TECH)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    rng = random.Random(0)
    paths: list[Path] = []
    for lane in range(1, lanes + 1):
        for mate, k in (("R1", "R1"), ("R2", "R2"), ("I1", None)):
            p = tmp_path / f"SRR9000001_S1_L{lane:03d}_{mate}_001.fastq.gz"
            if k is None:
                _write_fastq_gz(
                    p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                )
            else:
                _write_fastq_gz(p, list(reads[k]))
            paths.append(p)
    return spec, reg, paths


def test_a_multilane_run_absorbs_every_lane_into_its_role(tmp_path: Path) -> None:
    """GSE208154: one accession across N lanes -> one run of N*(R1+R2+I1). The injective assignment
    fills each role ONCE, so the surplus lanes were left unassigned and the run blocked with
    NO_VALID_ROLE_ASSIGNMENT. Now each surplus lane rejoins its role (barcode/cDNA) or is set aside
    (index), so the run resolves and every file is placed."""
    spec, reg, paths = _multilane_reads(tmp_path, lanes=3)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1  # one accession -> one run holding all 9 files
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed -- nothing left to block
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 3  # the 3 I1 lanes set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [3, 3]  # barcode and cDNA each carry all 3 lanes


def test_multilane_units_emit_every_lane_and_exclude_index(tmp_path: Path) -> None:
    """The point of absorption: units.tsv carries one row per lane per counted role (so STARsolo
    comma-joins them), the index lanes are excluded, and the manifest validates clean."""
    spec, reg, paths = _multilane_reads(tmp_path, lanes=3)
    manifest = _manifest(tmp_path, spec, reg, paths)

    assert all(f.read_id is not None for f in manifest.library.files)  # nothing unassigned
    assert sum(1 for f in manifest.library.files if f.read_id == INDEX_ROLE) == 3
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    # 3 lanes x 2 counted roles = 6 rows; each counted role appears once per lane.
    assert len(rows) == 6
    assert set(Counter(r["read_id"] for r in rows).values()) == {3}
    # Every lane is the SAME run, so `fastqs(sample, role)` collects and comma-joins them by path.
    assert len({r["run"] for r in rows}) == 1


# ------------------------------------------------------------ multi-flowcell surplus absorption


def _multiflowcell_reads(
    tmp_path: Path,
    flowcells: tuple[str, ...] = ("HCL2YBBXY", "HCL2KBBXY"),
    lanes: int = 2,
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession sequenced across several FLOWCELLS, each with ``lanes`` lanes of
    R1(28)+R2(90)+I1(8), named the bcl2fastq way with the flowcell id in the stem
    (``SRR..._<FC>_S1_L001_R1_001.fastq.gz``). The shared SRA accession groups every file into ONE run
    -- the GSE208154 shape (11 SRR runs x 2 flowcells x 8 lanes x {R1,R2,I1}). Because the flowcell id
    differs between files, de-laning left the cross-flowcell surplus with a different identity than its
    role representative, so it stayed unassigned and the run blocked; designation matching fuses them."""
    spec = kb.load_spec(TECH)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    rng = random.Random(0)
    paths: list[Path] = []
    for fc in flowcells:
        for lane in range(1, lanes + 1):
            for mate, k in (("R1", "R1"), ("R2", "R2"), ("I1", None)):
                p = tmp_path / f"SRR9000001_{fc}_S1_L{lane:03d}_{mate}_001.fastq.gz"
                if k is None:
                    _write_fastq_gz(
                        p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                    )
                else:
                    _write_fastq_gz(p, list(reads[k]))
                paths.append(p)
    return spec, reg, paths


def test_a_multiflowcell_run_absorbs_every_flowcell_into_its_role(tmp_path: Path) -> None:
    """GSE208154: one accession sequenced across 2 flowcells x 2 lanes -> one run of 4*(R1+R2+I1). The
    files differ by flowcell id, so 2026.7.4's de-lane equality could not fuse the cross-flowcell surplus
    (its de-laned name still carried the differing flowcell id) and the run blocked. Matching by read
    designation (R1/R2) + length fuses them, so every file is placed and the run resolves. FAILS before
    the fix (some files unassigned -> a blocker), passes after."""
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=("HCL2YBBXY", "HCL2KBBXY"), lanes=2)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1  # one accession -> one run holding all 12 files
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed across BOTH flowcells
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 4  # 2 flowcells x 2 lanes of I1 set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [4, 4]  # barcode and cDNA each carry all 4 (flowcell x lane) files


def test_multiflowcell_units_emit_every_file_and_validate_clean(tmp_path: Path) -> None:
    """End to end for the flowcell shape: every file placed -> the manifest validates clean and
    units.tsv carries one row per (flowcell x lane) per counted role (STARsolo comma-joins them),
    with the index files excluded."""
    spec, reg, paths = _multiflowcell_reads(tmp_path, flowcells=("HCL2YBBXY", "HCL2KBBXY"), lanes=2)
    manifest = _manifest(tmp_path, spec, reg, paths)

    assert all(f.read_id is not None for f in manifest.library.files)  # nothing unassigned
    assert sum(1 for f in manifest.library.files if f.read_id == INDEX_ROLE) == 4
    report = validate_manifest(manifest)
    assert report.ok, [b.message for b in report.blockers]
    assert exit_code_for_report(report) == 0

    rows = core._units(manifest)
    assert all(row["read_id"] != INDEX_ROLE for row in rows)
    # 4 (flowcell x lane) x 2 counted roles = 8 rows; each counted role appears once per file.
    assert len(rows) == 8
    assert set(Counter(r["read_id"] for r in rows).values()) == {4}
    assert len({r["run"] for r in rows}) == 1  # all one accession -> one run, comma-joined


# ------------------------------------------------------------ coverage over score (the real pathology)


#: A constant 25 bp head killing 5′ diversity in the cDNA role's ``distinct_ratio R2[0:20]`` window.
_FLAT_CDNA_HEAD = "ACGTACGTACGTACGTACGTACGTA"


def _barcode_role(spec: kb.Spec) -> str:
    return next(r.id for r in spec.reads if any(el.type == "barcode" for el in r.elements))


def _low_diversity_cdna_multilane(
    tmp_path: Path, lanes: int = 3
) -> tuple[kb.Spec, OnlistRegistry, list[Path]]:
    """One 10x v3 accession across ``lanes`` lanes, but every cDNA (R2) read shares a constant 5′ head
    -- the real GSE208154 shape the earlier synthetic missed. The cDNA role's only discriminator is
    ``distinct_ratio R2[0:20] high``; with a flat cDNA 5′ end, a whitelist-diverse 28 bp *barcode* read
    scores that discriminator ABOVE the 90 bp cDNA read. Score-max then seats a barcode read in the cDNA
    role and orphans every cDNA-length file (absorption cannot recover -- the cDNA rep is a barcode
    read), so the run blocks. The 90 bp reads are forbidden for the barcode role (dead zone), so the cDNA
    role is their only home; the coverage rule seats them there and the run resolves."""
    spec = kb.load_spec(TECH)
    reg = _registry_for(spec)
    reads = kb.generate_reads(spec, n=600, seed=0)
    flat_cdna = [
        _FLAT_CDNA_HEAD + r[len(_FLAT_CDNA_HEAD) :] for r in reads["R2"]
    ]  # length preserved
    rng = random.Random(0)
    paths: list[Path] = []
    for lane in range(1, lanes + 1):
        for mate, seqs in (("R1", reads["R1"]), ("R2", flat_cdna), ("I1", None)):
            p = tmp_path / f"SRR9000002_S1_L{lane:03d}_{mate}_001.fastq.gz"
            if seqs is None:
                _write_fastq_gz(
                    p, ["".join(rng.choice("ACGT") for _ in range(8)) for _ in range(600)]
                )
            else:
                _write_fastq_gz(p, list(seqs))
            paths.append(p)
    return spec, reg, paths


def test_a_low_diversity_cdna_multifile_run_resolves_by_coverage(tmp_path: Path) -> None:
    """The real GSE208154 pathology: low-diversity cDNA 5′ ends let a 28 bp barcode read out-score the
    90 bp cDNA read for the cDNA role. Score alone seats a barcode read in the cDNA role and orphans every
    cDNA-length file -> NO_VALID_ROLE_ASSIGNMENT. The coverage rule (the 90 bp reads are single-role
    eligible -- forbidden for barcode, so cDNA is their sole home) seats them and the run resolves.
    FAILS before the eligibility fix (cDNA-length files unassigned -> a blocker), passes after."""
    spec, reg, paths = _low_diversity_cdna_multilane(tmp_path, lanes=3)
    multi = resolve_runs(paths, registry=reg, use_cache=False)
    assert not multi.blockers
    assert len(multi.runs) == 1
    assert multi.runs[0].winner in {"10x-3p-gex-v3", "10x-3p-gex-v3.1"}

    role_of_sha = multi.role_of_sha()
    assert len(role_of_sha) == len(paths)  # every file placed -- nothing orphaned
    counts = Counter(role_of_sha.values())
    assert counts[INDEX_ROLE] == 3  # the 3 I1 lanes set aside
    non_index = sorted(c for r, c in counts.items() if r != INDEX_ROLE)
    assert non_index == [3, 3]  # barcode and cDNA each carry all 3 lanes

    # The cDNA role is filled by the long cDNA reads, not the 28 bp barcode reads (the whole bug).
    obs = {o.file.sha256: o for o in multi.observations}
    cdna_role = next(r for r in counts if r != INDEX_ROLE and r != _barcode_role(spec))
    cdna_modes = {obs[s].read_length.mode for s, r in role_of_sha.items() if r == cdna_role}
    assert all(m > 28 for m in cdna_modes)  # long reads only -- no 28 bp barcode read slipped in


def test_read_designation_reads_the_mate_across_lanes_and_flowcells() -> None:
    """Absorption fuses a surplus file into a role by the read designation the sequencer wrote, so that
    token must be read precisely -- and identically across the lanes and flowcells of one accession,
    whose files differ only by a lane token and the flowcell id the designation ignores."""
    from seqforge.resolve.engine import _read_designation

    # The Illumina R/I token, read regardless of lane or flowcell id.
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_R1_001.fastq.gz") == "R1"
    assert _read_designation("SRR1_HCL2YBBXY_S1_L002_R2_001.fastq.gz") == "R2"
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_I1_001.fastq.gz") == "I1"
    # The whole point: two flowcells / two lanes of one read share ONE designation (de-laning could not
    # -- the flowcell id differs, so their de-laned names differed and the surplus stayed unassigned).
    assert _read_designation("SRR1_HCL2YBBXY_S1_L001_R1_001.fastq.gz") == _read_designation(
        "SRR1_HCL2KBBXY_S1_L005_R1_001.fastq.gz"
    )
    # fasterq-dump's numeric mate suffix (SRR..._1 / _2 / _3).
    assert _read_designation("SRXidx_1.fastq.gz") == "1"
    assert _read_designation("SRXidx_2.fastq.gz") == "2"
    assert _read_designation("SRXidx_3.fastq.gz") == "3"
    # R1 and R2 are DIFFERENT designations, so a barcode surplus never rejoins the cDNA role.
    assert _read_designation("x_R1.fastq.gz") != _read_designation("x_R2.fastq.gz")
    # A name that declares no mate designation -> None, so it is never absorbed (stays a blocker).
    assert _read_designation("sample_barcodes.fastq.gz") is None


def test_a_clean_single_lane_run_is_unaffected_by_absorption(tmp_path: Path) -> None:
    """Regression: the absorption only fires on surplus lane siblings. A normal single-lane run (one
    R1 + one R2, no leftovers) is byte-identical to before -- no role is duplicated, nothing tagged."""
    spec, reg, paths = _reads(tmp_path, extra=None)
    out = resolve_dataset(paths, registry=reg, use_cache=False)
    roles = index_tagged_roles(out.result.candidates[0], out.observations)
    assert len(roles) == 2  # exactly the two assigned roles, nothing absorbed or tagged
    assert INDEX_ROLE not in roles.values()
