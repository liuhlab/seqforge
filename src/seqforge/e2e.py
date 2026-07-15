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
from .kb.generate import write_fastq_gz as _write_fastq_gz
from .manifest import (
    ExperimentInputs,
    ProcessingInputs,
    fill_manifest,
    fill_processing,
    validate_manifest,
)
from .models.dataset import SampleGroup
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


@dataclass(frozen=True)
class GeneModel:
    """A gene with BOTH what a cell sees and what a nucleus sees.

    ``mrna`` is the spliced, sense-strand transcript — a whole-cell library's cDNA. ``introns`` are
    sense-strand intron sequences: a nucleus is full of unspliced pre-mRNA, so a nuclear library reads
    these too. That difference is the entire reason ``--soloFeatures GeneFull`` exists, and the reason
    yeast cannot test it.
    """

    gene_id: str
    mrna: str
    introns: tuple[str, ...]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlaps(spans: list[tuple[int, int]], s: int, e: int) -> bool:
    """Does [s,e] touch any merged, sorted interval? Bisect on end coords."""
    import bisect

    i = bisect.bisect_left([sp[1] for sp in spans], s)
    return i < len(spans) and spans[i][0] <= e


def load_gene_models(
    fasta: Path,
    gtf: Path,
    *,
    min_len: int = 600,
    min_intron: int = 300,
    max_genes: int = 120,
    seed: int = 0,
) -> list[GeneModel]:
    """Build spliced mRNA **and clean intron sequences** per gene (the intron-rich / GeneFull fixture).

    "Clean" is doing real work here. A read is only unambiguously intronic if its intron overlaps no
    exon *anywhere* in the annotation and no *other* gene's span — otherwise STARsolo may legitimately
    assign it elsewhere or call it ambiguous, and the injected ground truth would be a fiction. So an
    intron qualifies only when it is long enough to contain a whole read, hits no exon genome-wide,
    and lies inside exactly one gene. Being strict here is what lets the assertion be exact rather
    than approximate — the same discipline as the unique-UMI trick in :func:`simulate`.
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

    # every exon, genome-wide (any biotype filter already applied) — the ambiguity mask
    all_exons: dict[str, list[tuple[int, int]]] = defaultdict(list)
    gene_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for parts in exons.values():
        chrom = parts[0][0]
        all_exons[chrom].extend((s, e) for _c, s, e, _st in parts)
        gene_spans[chrom].append(
            (min(s for _c, s, _e, _st in parts), max(e for _c, _s, e, _st in parts))
        )
    exon_mask = {c: _merge(v) for c, v in all_exons.items()}
    span_list = {c: sorted(v) for c, v in gene_spans.items()}

    models: list[GeneModel] = []
    for gene_id, parts in sorted(exons.items()):
        chrom, strand = parts[0][0], parts[0][3]
        if chrom not in chroms:
            continue
        merged = _merge([(s, e) for _c, s, e, _st in parts])
        mrna = "".join(chroms[chrom][s - 1 : e] for s, e in merged)  # GTF is 1-based inclusive
        if strand == "-":
            mrna = _revcomp(mrna)
        if len(mrna) < min_len or "N" in mrna:
            continue

        introns: list[str] = []
        for (_s1, e1), (s2, _e2) in zip(merged, merged[1:], strict=False):
            istart, iend = e1 + 1, s2 - 1
            if iend - istart + 1 < min_intron:
                continue
            if _overlaps(exon_mask[chrom], istart, iend):
                continue  # some other transcript exonifies this gap
            covering = [g for g in span_list[chrom] if g[0] <= iend and g[1] >= istart]
            if len(covering) != 1:
                continue  # another gene overlaps -> a read here is not unambiguously ours
            seq = chroms[chrom][istart - 1 : iend].upper()
            if "N" in seq:
                continue
            introns.append(_revcomp(seq) if strand == "-" else seq)
        if introns:
            models.append(GeneModel(gene_id=gene_id, mrna=mrna.upper(), introns=tuple(introns)))

    rng = random.Random(seed)
    rng.shuffle(models)
    return models[:max_genes]


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


@dataclass
class NucleiSimulation:
    """A single-NUCLEUS library: exonic + intronic reads, with the two truths kept apart.

    Keeping them apart is the whole design. ``Gene`` must recover ``truth_exonic`` and count NONE of
    the intronic reads; ``GeneFull`` must recover their sum. One matrix cannot satisfy both, so the
    pair pins the semantic difference that yeast (nearly intron-free) can never exercise.
    """

    cdna: list[str] = field(default_factory=list)
    barcode: list[str] = field(default_factory=list)
    truth_exonic: dict[tuple[str, str], int] = field(default_factory=dict)
    truth_intronic: dict[tuple[str, str], int] = field(default_factory=dict)
    whitelist: list[str] = field(default_factory=list)

    @property
    def truth_full(self) -> dict[tuple[str, str], int]:
        """What GeneFull must see: pre-mRNA = exons + introns."""
        out = dict(self.truth_exonic)
        for k, v in self.truth_intronic.items():
            out[k] = out.get(k, 0) + v
        return out


def simulate_nuclei(
    models: list[GeneModel],
    *,
    n_cells: int = 8,
    reads_per_cell: int = 250,
    intron_frac: float = 0.4,
    read_len: int = 90,
    cb_len: int = 16,
    umi_len: int = 12,
    seed: int = 0,
) -> NucleiSimulation:
    """Emit 10x-3'-v3-shaped reads from **pre-mRNA**: a fraction land in introns, as in real nuclei.

    Same geometry and unique-UMI discipline as :func:`simulate`, so the injected count per (cell, gene)
    is exactly the read count. Intronic reads are taken whole from a clean intron, sense to the gene,
    so ``soloStrand Forward`` treats them exactly like exonic ones — the ONLY thing under test here is
    exon-vs-full counting, and nothing else is allowed to vary.
    """
    rng = random.Random(seed)
    bases = "ACGT"
    whitelist = sorted(
        {"".join(rng.choice(bases) for _ in range(cb_len)) for _ in range(n_cells * 4)}
    )
    cells = whitelist[:n_cells]
    usable = [m for m in models if any(len(i) >= read_len for i in m.introns)]
    if not usable:
        raise E2EUnavailable("no gene has an intron long enough to hold a read")

    sim = NucleiSimulation(whitelist=whitelist)
    exonic: dict[tuple[str, str], int] = defaultdict(int)
    intronic: dict[tuple[str, str], int] = defaultdict(int)
    seen_umis: set[str] = set()
    for cell in cells:
        for _ in range(reads_per_cell):
            model = usable[rng.randrange(len(usable))]
            if rng.random() < intron_frac:
                pool = [i for i in model.introns if len(i) >= read_len]
                intron = pool[rng.randrange(len(pool))]
                start = rng.randrange(0, len(intron) - read_len + 1)
                sim.cdna.append(intron[start : start + read_len])
                intronic[(cell, model.gene_id)] += 1
            else:
                mrna = model.mrna
                tail = min(len(mrna), 500)  # 3' bias, as in simulate()
                start = rng.randrange(len(mrna) - tail, len(mrna) - read_len + 1)
                sim.cdna.append(mrna[start : start + read_len])
                exonic[(cell, model.gene_id)] += 1

            while True:  # unique UMIs => injected count == read count, so the assert is exact
                umi = "".join(rng.choice(bases) for _ in range(umi_len))
                if umi not in seen_umis:
                    seen_umis.add(umi)
                    break
            sim.barcode.append(cell + umi)
    sim.truth_exonic = dict(exonic)
    sim.truth_intronic = dict(intronic)
    return sim


def write_fastq_gz(path: Path, seqs: list[str], prefix: str) -> None:
    """Reproducible fastq.gz. Delegates to the KB's single writer (mtime-pinned; see its docstring)."""
    _write_fastq_gz(path, seqs, prefix=prefix)


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
        # --soloFeatures takes N space-separated values; STAR writes one Solo.out/<feature>/ per value.
        *("--soloFeatures", *_feature_list(solo["soloFeatures"])),
        "--outFileNamePrefix",
        f"{outdir}/",
        "--outSAMtype",
        "None",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise E2EUnavailable(f"STAR failed ({proc.returncode}): {proc.stderr[-2000:]}")
    return outdir / "Solo.out" / _feature_list(solo["soloFeatures"])[0] / "raw"


def _feature_list(value: object) -> list[str]:
    """KB params carry soloFeatures as a list or a string; STAR's CLI wants separate argv items."""
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return str(value).split()


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
        seqforge_version=__version__,
    )
    report = validate_manifest(manifest)
    if not report.ok:
        return {"passed": False, "stage": "validate", "blockers": [b.code for b in report.blockers]}

    # --- compose emits the params; the aligner runs with exactly those ---
    processing = fill_processing(
        spec=spec,
        dataset=manifest,
        processing=ProcessingInputs(assembly=assets.assembly, annotation_name=assets.annotation),
        seqforge_version=__version__,
    )
    composed = compose_plan(manifest, processing, registry=registry)
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


def run_intron_e2e(
    assets: E2EAssets,
    *,
    workdir: Path,
    n_cells: int = 8,
    reads_per_cell: int = 250,
    intron_frac: float = 0.4,
    threads: int = 8,
    seed: int = 0,
    min_recovery: float = 0.90,
) -> dict[str, object]:
    """The intron-rich / **GeneFull** gate (design §4.1 coverage caveat; needs ce11, not sacCer3).

    Yeast is nearly intron-free, so ``Gene`` and ``GeneFull`` are indistinguishable on it and the
    existing e2e certifies neither. This one injects a known number of **intronic** reads — what a
    single-NUCLEUS library actually contains — and asserts the two counting rules disagree in exactly
    the way they must:

    - ``Gene``     recovers the exonic truth and counts **none** of the intronic reads.
    - ``GeneFull`` recovers exonic + intronic.

    Both matrices come from **one** STARsolo run (``--soloFeatures Gene GeneFull``), so the alignment
    is bit-identical between them and the only thing that can differ is the counting rule. If they
    ever agreed on this data, the fixture would be broken, not passing — hence ``genefull_exceeds_gene``.

    It also produces the number that matters for the design: ``gene_signal_lost`` is the fraction of a
    nuclear library that ``--soloFeatures Gene`` silently throws away. STARsolo exits 0 either way and
    the matrix merely looks like a thin dataset — the same failure shape as a strand inversion, and
    exactly the class §4.1 exists to catch.

    This gate runs on the **compiler's own params**: no override. It used to force
    ``soloFeatures = [Gene, GeneFull]`` past a compiler that would have emitted ``Gene``, and its
    docstring had to admit the fixture "does NOT prove the compiler would choose GeneFull, because
    today it cannot". It can now (R14/R15), so ``gene_signal_lost`` stops measuring our own bug and
    starts measuring a **counterfactual**: what Gene-only would have cost, on a run where we did not
    do it.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    models = load_gene_models(assets.fasta, assets.gtf, seed=seed)
    if not models:
        raise E2EUnavailable(
            f"no gene in {assets.assembly} has a clean intron long enough to hold a read "
            "(is this an intron-poor assembly? the fixture needs ce11, not sacCer3)"
        )
    sim = simulate_nuclei(
        models,
        n_cells=n_cells,
        reads_per_cell=reads_per_cell,
        intron_frac=intron_frac,
        seed=seed,
    )

    cdna_fq = workdir / "sim_R2.fastq.gz"
    bc_fq = workdir / "sim_R1.fastq.gz"
    write_fastq_gz(cdna_fq, sim.cdna, "SIM")
    write_fastq_gz(bc_fq, sim.barcode, "SIM")

    spec = load_spec(E2E_TECH)
    registry = OnlistRegistry(offline=True)
    registry.register_synthetic("3M-february-2018", sim.whitelist)

    # drive the real compiler for the params, exactly as run_e2e does
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
            organism_taxid=6239,  # C. elegans — the intron-rich fixture's organism
            samples=[SampleGroup(sample_id="s1", file_uris=[p.name for p in (bc_fq, cdna_fq)])],
        ),
        seqforge_version=__version__,
    )
    report = validate_manifest(manifest)
    if not report.ok:
        return {"passed": False, "stage": "validate", "blockers": [b.code for b in report.blockers]}

    processing = fill_processing(
        spec=spec,
        dataset=manifest,
        processing=ProcessingInputs(assembly=assets.assembly, annotation_name=assets.annotation),
        seqforge_version=__version__,
    )
    composed = compose_plan(manifest, processing, registry=registry)
    solo = dict(composed.config["solo"])  # type: ignore[arg-type]
    composed_features = _feature_list(solo["soloFeatures"])
    # No override. The compiler's own params run, and both Gene and GeneFull are among them because
    # the default counts everything (R15). If that ever regresses, this gate cannot even read its own
    # matrices — which is the point of asserting it here rather than trusting the default.
    if not {"Gene", "GeneFull"} <= set(composed_features):
        return {
            "passed": False,
            "stage": "compose",
            "reason": "the compiler no longer emits both Gene and GeneFull",
            "composed_soloFeatures": composed_features,
        }

    wl_path = workdir / "whitelist.txt"
    wl_path.write_text("\n".join(sorted(sim.whitelist)) + "\n")
    outdir = workdir / "star_intron"
    run_starsolo(
        assets,
        cdna_fq=cdna_fq,
        barcode_fq=bc_fq,
        whitelist=wl_path,
        solo=solo,
        outdir=outdir,
        threads=threads,
    )

    gene = parse_solo_matrix(outdir / "Solo.out" / "Gene" / "raw")
    full = parse_solo_matrix(outdir / "Solo.out" / "GeneFull" / "raw")
    v_gene = _compare(sim.truth_exonic, gene)
    v_full = _compare(sim.truth_full, full)

    n_exonic = sum(sim.truth_exonic.values())
    n_intronic = sum(sim.truth_intronic.values())
    total = n_exonic + n_intronic
    gene_total, full_total = sum(gene.values()), sum(full.values())

    # Gene must not count intronic reads: its total may not meaningfully exceed the exonic truth.
    # (<=1.02x rather than <= exactly: STAR can place a rare read ambiguously either way.)
    gene_excludes_introns = gene_total <= n_exonic * 1.02
    genefull_exceeds_gene = full_total > gene_total
    recovery_gene = float(v_gene["recovery_rate"])  # type: ignore[arg-type]
    recovery_full = float(v_full["recovery_rate"])  # type: ignore[arg-type]

    passed = (
        v_gene["n_spurious_pairs"] == 0
        and v_full["n_spurious_pairs"] == 0
        and v_full["n_inflated_pairs"] == 0
        and gene_excludes_introns
        and genefull_exceeds_gene
        and recovery_gene >= min_recovery
        and recovery_full >= min_recovery
    )
    return {
        "passed": bool(passed),
        "stage": "counts",
        "assembly": assets.assembly,
        "decided": decided,
        "n_gene_models": len(models),
        "n_cells": n_cells,
        "injected_exonic": n_exonic,
        "injected_intronic": n_intronic,
        "gene_total": gene_total,
        "genefull_total": full_total,
        "recovery_gene_vs_exonic": round(recovery_gene, 4),
        "recovery_genefull_vs_full": round(recovery_full, 4),
        "gene_excludes_introns": gene_excludes_introns,
        "genefull_exceeds_gene": genefull_exceeds_gene,
        # THE HEADLINE, and it is now a COUNTERFACTUAL: what --soloFeatures Gene alone WOULD have
        # discarded from this nuclear library, measured on a run that did not discard it.
        "gene_signal_lost": round(1 - (gene_total / total), 4) if total else 0.0,
        # what the real compiler emitted — no override (R15). This is the assertion.
        "composed_soloFeatures": composed_features,
        "primary_feature": composed.config.get("primary_feature"),
        "star": star_stats(outdir),
        "gene_verdict": v_gene,
        "genefull_verdict": v_full,
    }


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
