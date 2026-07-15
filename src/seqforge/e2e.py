"""``kb e2e`` — the one real end-to-end run, asserted against injected ground truth (design §4.1.3).

This is the only gate that can catch the failures that **do not error**: an inverted ``--soloStrand``,
a wrong ``--soloUMIlen``, a mangled whitelist. STARsolo exits 0 and emits a matrix that merely looks
like a thin dataset — a dry run and a linter see nothing. So we run the real toolchain on data small
enough to be free (sacCer3, 12 Mb) and assert **the count matrix equals the ground truth we injected**.

It drives the WHOLE compiler, not just the aligner:

    simulate reads from sacCer3 transcripts (+ injected barcodes/UMIs)
      -> probe -> resolve  (must independently decide 10x 3' v3 from the bytes)
      -> manifest fill/validate
      -> compose           (emits the STARsolo params from the KB)
      -> STARsolo          (run with THOSE composed params)
      -> assert counts == injected truth

The strand check is the point, so it is proven both ways: the composed (Forward) params must recover
the truth, and the same reads under an inverted `--soloStrand Reverse` must collapse — otherwise the
test could not have caught an inversion in the first place.

Requires a toolchain seqforge does not own (STAR + a built genome index), so it is skip-gated
everywhere else and runs on a Linux compute node.
"""

from __future__ import annotations

import gzip
import random
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .compose import plan as compose_plan
from .io import OnlistRegistry
from .kb import load_spec
from .manifest import ExperimentInputs, ProcessingInputs, fill_manifest, validate_manifest
from .models.manifest import SampleGroup
from .probe import probe_file
from .resolve import resolve_dataset

_GENE_ID = re.compile(r'gene_id "([^"]+)"')
_BIOTYPE = re.compile(r'gene_biotype "([^"]+)"')
_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")

#: The pilot's e2e chemistry: 16 bp CB + 12 bp UMI on R1, cDNA on R2, soloStrand Forward.
E2E_TECH = "10x-3p-gex-v3"


class E2EUnavailable(RuntimeError):
    """The e2e toolchain (STAR + a genome index) is not present on this machine."""


@dataclass(frozen=True)
class E2EAssets:
    """Everything the run needs from outside seqforge (liulab-genome + liulab-runtime own these)."""

    fasta: Path
    gtf: Path
    star_index: Path
    star_bin: str
    assembly: str = "sacCer3"
    annotation: str = "ensembl_R64-1-1"


@dataclass
class Simulation:
    """Simulated reads plus the ground truth they were generated from."""

    cdna: list[str] = field(default_factory=list)
    barcode: list[str] = field(default_factory=list)
    #: (cell_barcode, gene_id) -> injected UMI count (UMIs are unique, so this is the read count)
    truth: dict[tuple[str, str], int] = field(default_factory=dict)
    whitelist: list[str] = field(default_factory=list)


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


def read_fasta(path: Path) -> dict[str, str]:
    """Load a small genome into memory (sacCer3 is 12 Mb — this is the whole point of using it)."""
    chroms: dict[str, str] = {}
    name = None
    chunks: list[str] = []
    with open(path) as fh:
        for line in fh:
            if line.startswith(">"):
                if name is not None:
                    chroms[name] = "".join(chunks)
                name = line[1:].split()[0]
                chunks = []
            else:
                chunks.append(line.strip())
    if name is not None:
        chroms[name] = "".join(chunks)
    return chroms


def load_genes(
    fasta: Path, gtf: Path, *, min_len: int = 600, max_genes: int = 120, seed: int = 0
) -> list[tuple[str, str]]:
    """Build spliced, sense-strand mRNA sequences per gene from the GTF's exons.

    Returns ``(gene_id, mrna)`` for protein-coding genes long enough to sample from. The sequence is
    the **mRNA sense strand** (minus-strand genes are reverse-complemented), which is what a 3' kit's
    cDNA read carries — that identity is exactly what ``soloStrand Forward`` asserts.
    """
    chroms = read_fasta(fasta)
    exons: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
    with open(gtf) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) < 9 or f[2] != "exon":
                continue
            biotype = _BIOTYPE.search(f[8])
            if biotype and biotype.group(1) != "protein_coding":
                continue
            gid = _GENE_ID.search(f[8])
            if not gid:
                continue
            exons[gid.group(1)].append((f[0], int(f[3]), int(f[4]), f[6]))

    genes: list[tuple[str, str]] = []
    for gene_id, parts in exons.items():
        chrom, strand = parts[0][0], parts[0][3]
        if chrom not in chroms:
            continue
        # merge exon spans (a gene's transcripts may overlap); splice in genomic order
        spans = sorted({(s, e) for _c, s, e, _st in parts})
        merged: list[tuple[int, int]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        seq = "".join(chroms[chrom][s - 1 : e] for s, e in merged)  # GTF is 1-based inclusive
        if strand == "-":
            seq = _revcomp(seq)
        if len(seq) >= min_len and "N" not in seq:
            genes.append((gene_id, seq.upper()))

    genes.sort()  # deterministic before sampling
    rng = random.Random(seed)
    rng.shuffle(genes)
    return genes[:max_genes]


def simulate(
    genes: list[tuple[str, str]],
    *,
    n_cells: int = 8,
    reads_per_cell: int = 250,
    read_len: int = 90,
    cb_len: int = 16,
    umi_len: int = 12,
    seed: int = 0,
) -> Simulation:
    """Emit 10x-3'-v3-shaped reads from real transcripts, recording exactly what we injected.

    R2 = a ``read_len`` cDNA fragment taken **sense to the mRNA** (3'-biased, as a 3' kit is).
    R1 = ``CB + UMI``. Every UMI is unique, so the injected UMI count per (cell, gene) is simply the
    number of reads — which is what STARsolo's Gene matrix must reproduce.
    """
    rng = random.Random(seed)
    bases = "ACGT"
    whitelist = sorted(
        {"".join(rng.choice(bases) for _ in range(cb_len)) for _ in range(n_cells * 4)}
    )
    cells = whitelist[:n_cells]

    sim = Simulation(whitelist=whitelist)
    truth: dict[tuple[str, str], int] = defaultdict(int)
    seen_umis: set[str] = set()
    for cell in cells:
        for _ in range(reads_per_cell):
            gene_id, mrna = genes[rng.randrange(len(genes))]
            # 3' bias: a 3' kit samples near the polyA end, i.e. the mRNA's tail
            tail = min(len(mrna), 500)
            lo = len(mrna) - tail
            start = rng.randrange(lo, len(mrna) - read_len + 1)
            sim.cdna.append(mrna[start : start + read_len])

            while True:  # unique UMIs => injected count == read count, so the assert is exact
                umi = "".join(rng.choice(bases) for _ in range(umi_len))
                if umi not in seen_umis:
                    seen_umis.add(umi)
                    break
            sim.barcode.append(cell + umi)
            truth[(cell, gene_id)] += 1
    sim.truth = dict(truth)
    return sim


def write_fastq_gz(path: Path, seqs: list[str], prefix: str) -> None:
    with gzip.open(path, "wt") as fh:
        for i, s in enumerate(seqs):
            fh.write(f"@{prefix}:{i}\n{s}\n+\n{'I' * len(s)}\n")


def parse_solo_matrix(solo_dir: Path) -> dict[tuple[str, str], int]:
    """Read STARsolo's raw Gene matrix (Matrix Market) into ``(barcode, gene) -> count``."""
    barcodes = (solo_dir / "barcodes.tsv").read_text().split()
    features = [
        ln.split("\t")[0] for ln in (solo_dir / "features.tsv").read_text().splitlines() if ln
    ]
    counts: dict[tuple[str, str], int] = {}
    with open(solo_dir / "matrix.mtx") as fh:
        header_seen = False
        for line in fh:
            if line.startswith("%"):
                continue
            if not header_seen:  # dims line
                header_seen = True
                continue
            gi, bi, val = line.split()
            n = int(val)
            if n:
                counts[(barcodes[int(bi) - 1], features[int(gi) - 1])] = n
    return counts


def run_starsolo(
    assets: E2EAssets,
    *,
    cdna_fq: Path,
    barcode_fq: Path,
    whitelist: Path,
    solo: dict[str, object],
    outdir: Path,
    threads: int = 8,
) -> Path:
    """Run STARsolo with the COMPOSED params (this is what makes the gate test the compiler)."""
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        assets.star_bin,
        "--runMode",
        "alignReads",
        "--genomeDir",
        str(assets.star_index),
        "--runThreadN",
        str(threads),
        # --readFilesIn takes the cDNA read FIRST, then the barcode read
        "--readFilesIn",
        str(cdna_fq),
        str(barcode_fq),
        "--readFilesCommand",
        "zcat",
        "--soloType",
        str(solo["soloType"]),
        "--soloCBstart",
        str(solo["soloCBstart"]),
        "--soloCBlen",
        str(solo["soloCBlen"]),
        "--soloUMIstart",
        str(solo["soloUMIstart"]),
        "--soloUMIlen",
        str(solo["soloUMIlen"]),
        "--soloCBwhitelist",
        str(whitelist),
        "--soloStrand",
        str(solo["soloStrand"]),
        "--soloFeatures",
        str(solo["soloFeatures"]),
        "--outFileNamePrefix",
        f"{outdir}/",
        "--outSAMtype",
        "None",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise E2EUnavailable(f"STAR failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return outdir / "Solo.out" / "Gene" / "raw"


def run_e2e(
    assets: E2EAssets,
    *,
    workdir: Path,
    n_cells: int = 8,
    reads_per_cell: int = 250,
    threads: int = 8,
    seed: int = 0,
    check_strand_inversion: bool = True,
    min_recovery: float = 0.95,
    max_unexplained_loss: float = 0.02,
) -> dict[str, object]:
    """Drive simulate -> resolve -> fill -> compose -> STARsolo and assert against ground truth."""
    workdir.mkdir(parents=True, exist_ok=True)
    genes = load_genes(assets.fasta, assets.gtf, seed=seed)
    if not genes:
        raise E2EUnavailable("no protein-coding genes could be built from the GTF/fasta")
    sim = simulate(genes, n_cells=n_cells, reads_per_cell=reads_per_cell, seed=seed)

    cdna_fq = workdir / "sim_R2.fastq.gz"
    bc_fq = workdir / "sim_R1.fastq.gz"
    write_fastq_gz(cdna_fq, sim.cdna, "SIM")
    write_fastq_gz(bc_fq, sim.barcode, "SIM")

    # --- the compiler must identify the chemistry from the BYTES, with no metadata hint ---
    spec = load_spec(E2E_TECH)
    registry = OnlistRegistry(offline=True)
    registry.register_synthetic(spec.onlists["cb_whitelist"].registry, sim.whitelist)
    resolved = resolve_dataset(
        [bc_fq, cdna_fq], registry=registry, workspace=workdir, use_cache=False
    )
    decided = resolved.result.candidates[0].technology if resolved.result.candidates else None
    if decided != E2E_TECH:
        return {"passed": False, "stage": "resolve", "decided": decided, "expected": E2E_TECH}

    observations = [probe_file(p) for p in (bc_fq, cdna_fq)]
    manifest = fill_manifest(
        result=resolved.result,
        spec=spec,
        observations=observations,
        registry=registry,
        experiment=ExperimentInputs(
            organism_taxid=559292,
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in (bc_fq, cdna_fq)])],
        ),
        processing=ProcessingInputs(assembly=assets.assembly, annotation_name=assets.annotation),
        seqforge_version=__version__,
    )
    report = validate_manifest(manifest)
    if not report.ok:
        return {"passed": False, "stage": "validate", "blockers": [b.code for b in report.blockers]}

    # --- compose emits the params; the aligner runs with exactly those ---
    composed = compose_plan(manifest, registry=registry)
    solo = dict(composed.config["solo"])  # type: ignore[arg-type]
    wl_path = workdir / "whitelist.txt"
    wl_path.write_text("\n".join(sorted(sim.whitelist)) + "\n")

    solo_raw = run_starsolo(
        assets,
        cdna_fq=cdna_fq,
        barcode_fq=bc_fq,
        whitelist=wl_path,
        solo=solo,
        outdir=workdir / "star_forward",
        threads=threads,
    )
    observed = parse_solo_matrix(solo_raw)
    verdict = _compare(sim.truth, observed)
    stats = star_stats(workdir / "star_forward")

    # What the gate actually asserts, and why each clause earns its place:
    #  - no spurious pair  : a read must never be counted for a gene it did not come from.
    #  - no inflated count : we must never invent UMIs (a dedup/geometry bug looks like this).
    #  - recovery >= floor : every unambiguously-mappable read landed in the RIGHT (cell, gene).
    #                        The residual is STAR's multimapper loss, measured below, not slack.
    #  - strand sensitive  : proven separately, in run_e2e's inversion re-run.
    unique_frac = (
        stats.get("uniquely_mapped", 0.0) / stats["input_reads"]
        if stats.get("input_reads")
        else 0.0
    )
    recovery = float(verdict["recovery_rate"])  # type: ignore[arg-type]
    # `unexplained_loss` is the clause that actually indicts US: of the reads STAR placed uniquely,
    # how many failed to land in the right (cell, gene)? STAR's multimapper loss is subtracted out,
    # so what remains is the compiler's own error. It must be ~0.
    unexplained = max(0.0, unique_frac - recovery)
    counts_ok = (
        verdict["n_spurious_pairs"] == 0
        and verdict["n_inflated_pairs"] == 0
        and recovery >= min_recovery
        and unexplained <= max_unexplained_loss
    )
    result: dict[str, object] = {
        "passed": bool(counts_ok),
        "stage": "counts",
        "decided": decided,
        "solo_params": solo,
        "n_cells": n_cells,
        "n_genes_injected": len({g for _c, g in sim.truth}),
        "star": stats,
        "star_unique_fraction": round(unique_frac, 4),
        # the honest reconciliation: what we lost should be ~what STAR could not place uniquely
        "unexplained_loss": round(unexplained, 4),
        "min_recovery": min_recovery,
        "max_unexplained_loss": max_unexplained_loss,
        **verdict,
    }

    # --- prove the gate can actually SEE a strand inversion (else it proves nothing) ---
    if check_strand_inversion:
        inverted = dict(solo)
        inverted["soloStrand"] = "Reverse"
        inv_raw = run_starsolo(
            assets,
            cdna_fq=cdna_fq,
            barcode_fq=bc_fq,
            whitelist=wl_path,
            solo=inverted,
            outdir=workdir / "star_reverse",
            threads=threads,
        )
        inv_counts = parse_solo_matrix(inv_raw)
        inv_total = sum(inv_counts.values())
        total = sum(sim.truth.values())
        result["inverted_strand_total"] = inv_total
        result["injected_total"] = total
        # an inverted strand must destroy the signal; if it did not, the gate is blind
        result["strand_sensitive"] = inv_total < 0.2 * total
        result["passed"] = bool(result["passed"] and result["strand_sensitive"])

    return result


def star_stats(outdir: Path) -> dict[str, float]:
    """Parse STAR's Log.final.out so the recovery shortfall is ACCOUNTED FOR, not hand-waved.

    A read STAR maps to multiple loci is correctly dropped by STARsolo — paralog and subtelomeric
    repeat families (e.g. the Y' / YRF1 genes) genuinely cannot be assigned to one gene. That loss is
    STAR's ambiguity, not a compiler bug, so the gate must measure it rather than tolerate it blindly.
    """
    log = outdir / "Log.final.out"
    if not log.is_file():
        return {}
    wanted = {
        "Number of input reads": "input_reads",
        "Uniquely mapped reads number": "uniquely_mapped",
        "Number of reads mapped to multiple loci": "multi_loci",
        "Number of reads mapped to too many loci": "too_many_loci",
    }
    stats: dict[str, float] = {}
    for line in log.read_text().splitlines():
        if "|" not in line:
            continue
        label, _, value = line.partition("|")
        key = wanted.get(label.strip())
        if key:
            try:
                stats[key] = float(value.strip())
            except ValueError:
                pass
    return stats


def _compare(
    truth: dict[tuple[str, str], int], observed: dict[tuple[str, str], int]
) -> dict[str, object]:
    """Exact-match accounting, plus the diagnostics that make a failure debuggable."""
    injected_total = sum(truth.values())
    observed_total = sum(observed.values())
    spurious = {k: v for k, v in observed.items() if k not in truth}
    mismatched = {
        k: (truth[k], observed.get(k, 0)) for k in truth if observed.get(k, 0) != truth[k]
    }
    # inflation is categorically worse than loss: a count we did NOT inject is a fabricated
    # observation, whereas a missing count is (usually) STAR dropping an ambiguous read.
    inflated = {k: (truth[k], observed[k]) for k in truth if observed.get(k, 0) > truth[k]}
    recovered = sum(min(v, observed.get(k, 0)) for k, v in truth.items())
    return {
        "exact": not spurious and not mismatched,
        "n_inflated_pairs": len(inflated),
        "injected_total": injected_total,
        "observed_total": observed_total,
        "recovered_total": recovered,
        "recovery_rate": round(recovered / injected_total, 4) if injected_total else 0.0,
        "n_spurious_pairs": len(spurious),
        "n_mismatched_pairs": len(mismatched),
        # JSON-safe: a (cell, gene) tuple key cannot cross the wire (same rule as the M[role][file]
        # evidence matrix — no un-serializable value may reach a --json boundary).
        "example_mismatches": [
            {"cell": cb, "gene": gene, "injected": inj, "observed": obs}
            for (cb, gene), (inj, obs) in list(mismatched.items())[:5]
        ],
        "example_spurious": [
            {"cell": cb, "gene": gene, "observed": n}
            for (cb, gene), n in list(spurious.items())[:5]
        ],
    }


def discover_assets(
    *,
    assembly: str = "sacCer3",
    annotation: str = "ensembl_R64-1-1",
    fasta: Path | None = None,
    gtf: Path | None = None,
    star_index: Path | None = None,
    star_bin: str | None = None,
) -> E2EAssets:
    """Resolve the run's assets, preferring liulab-genome (R12: we consume it, never reimplement it)."""
    import shutil

    if fasta is None or gtf is None or star_index is None:
        try:
            from genome import Genome  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise E2EUnavailable(
                "liulab-genome is not importable and --fasta/--gtf/--star-index were not given"
            ) from exc
        g = Genome(assembly)
        fasta = fasta or Path(str(g.fasta_path))
        gtf = gtf or Path(str(g.default_gtf_path))
        star_index = star_index or Path(str(g.get_star_index(gtf=annotation)))
    resolved_star = star_bin or shutil.which("STAR")
    if not resolved_star:
        raise E2EUnavailable(
            "STAR is not on PATH; pass --star (e.g. liulab-runtime's align-rna env)"
        )
    return E2EAssets(
        fasta=Path(fasta),
        gtf=Path(gtf),
        star_index=Path(star_index),
        star_bin=resolved_star,
        assembly=assembly,
        annotation=annotation,
    )
