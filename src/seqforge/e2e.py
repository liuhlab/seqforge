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
import json
import os
import random
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import product as _product
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
#: Ensembl and WormBase spell it ``gene_biotype``; GENCODE spells it ``gene_type``. Match both.
#:
#: This was ``gene_biotype`` alone until hg38 arrived, and every assembly the gates had used until
#: then (sacCer3/ensembl_R64-1-1, ce11/WS298) is Ensembl-flavoured — so the pattern was never wrong
#: in a run anyone made. On a GENCODE GTF it matches nothing, and because the filter below reads
#: ``if biotype and ...``, matching nothing meant *filtering* nothing: it failed OPEN, silently
#: admitting every lncRNA and pseudogene to a fixture whose docstring promises protein-coding genes.
#: :func:`_parse_exons` now refuses a GTF it cannot filter rather than quietly widening.
_BIOTYPE = re.compile(r'gene_(?:bio)?type "([^"]+)"')
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


def _parse_exons(
    gtf: Path, *, biotype: str = "protein_coding"
) -> dict[str, list[tuple[str, int, int, str]]]:
    """Collect ``gene_id -> [(chrom, start, end, strand)]`` for exons of the wanted biotype.

    **This refuses rather than widens.** A GTF whose exon lines carry no recognized biotype attribute
    at all cannot be filtered, and the honest options are to error or to silently keep everything.
    Keeping everything is what the Ensembl-only pattern used to do on a GENCODE GTF, and it is the
    worse failure precisely because it does not look like one: the fixture still builds, the gate
    still passes, and the gene universe is quietly the wrong one. So we raise.
    """
    exons: dict[str, list[tuple[str, int, int, str]]] = defaultdict(list)
    n_exon_lines = 0
    n_typed = 0
    with open(gtf) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            f = line.rstrip("\n").split("\t")
            if len(f) < 9 or f[2] != "exon":
                continue
            n_exon_lines += 1
            found = _BIOTYPE.search(f[8])
            if found is not None:
                n_typed += 1
                if found.group(1) != biotype:
                    continue
            gid = _GENE_ID.search(f[8])
            if not gid:
                continue
            exons[gid.group(1)].append((f[0], int(f[3]), int(f[4]), f[6]))
    if n_exon_lines and not n_typed:
        raise E2EUnavailable(
            f"{gtf} has {n_exon_lines} exon lines and not one carries `gene_biotype` or `gene_type`, "
            f"so the {biotype!r} filter would pass every biotype through and the fixture would be "
            "built from an annotation we cannot filter. Refusing instead of silently widening."
        )
    return exons


def load_genes(
    fasta: Path, gtf: Path, *, min_len: int = 600, max_genes: int = 120, seed: int = 0
) -> list[tuple[str, str]]:
    """Build spliced, sense-strand mRNA sequences per gene from the GTF's exons.

    Returns ``(gene_id, mrna)`` for protein-coding genes long enough to sample from. The sequence is
    the **mRNA sense strand** (minus-strand genes are reverse-complemented), which is what a 3' kit's
    cDNA read carries — that identity is exactly what ``soloStrand Forward`` asserts.
    """
    chroms = read_fasta(fasta)
    exons = _parse_exons(gtf)

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
    exons = _parse_exons(gtf)

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
    cost: dict[str, object] | None = None,
    timeout: int = 1800,
    extra_args: tuple[str, ...] = (),
) -> Path:
    """Run STARsolo with the COMPOSED params (this is what makes the gate test the compiler).

    ``cost``, if given, is populated with this STAR run's wall-clock and peak RSS. It is an
    out-param rather than a return value because every existing caller wants the matrix path and
    nothing else; the measurement is a side channel for the callers that are pricing a default.
    """
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
        *extra_args,
    ]
    code, elapsed, maxrss_kib, err_tail = _run_measured(cmd, outdir=outdir, timeout=timeout)
    if code != 0:
        raise E2EUnavailable(f"STAR failed ({code}): {err_tail}")
    if cost is not None:
        # Linux reports ru_maxrss in KiB (macOS in bytes) — arc is Linux, and a cross-platform unit
        # guess here would be a fabricated number, so record the raw value and its unit.
        cost["star_wall_s"] = round(elapsed, 2)
        cost["star_peak_rss_gb"] = round(maxrss_kib / 1024 / 1024, 3)
        cost["star_peak_rss_kib"] = maxrss_kib
        cost["soloFeatures"] = _feature_list(solo["soloFeatures"])
    return outdir / "Solo.out" / _feature_list(solo["soloFeatures"])[0] / "raw"


def _run_measured(cmd: list[str], *, outdir: Path, timeout: int) -> tuple[int, float, int, str]:
    """Run ``cmd`` to completion; return ``(exit_code, wall_s, peak_rss_kib, stderr_tail)``.

    ``os.wait4`` reports rusage for **the one child it reaps**, and that is the whole reason this
    exists. The measurement here used to be ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss``, which
    is a high-water mark over *every* child the process has ever reaped — so a second STAR run in the
    same process inherits the first one's peak and can never report a smaller number. That was
    invisible while ``kb e2e-introns`` ran STAR exactly once, and its comment said as much: correct
    "because STAR is the only heavy child". A sweep runs STAR many times in one process, and every
    point after the first would have been silently ``max()``-ed with its predecessors — an increasing
    curve that would look exactly like the memory growth we are trying to measure. The assumption was
    load-bearing, asserted by a comment, and enforced by nothing.

    Output goes to files rather than pipes on purpose: ``Popen.communicate`` reaps the child itself,
    which would leave ``wait4`` nothing to collect rusage from.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    err_log = outdir / "star.stderr.log"
    started = time.monotonic()
    with open(outdir / "star.stdout.log", "w") as out_fh, open(err_log, "w") as err_fh:
        proc = subprocess.Popen(cmd, stdout=out_fh, stderr=err_fh)
        deadline = started + timeout
        while True:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
            if pid != 0:
                break
            if time.monotonic() > deadline:
                proc.kill()
                os.wait4(proc.pid, 0)
                proc.returncode = -9
                raise E2EUnavailable(f"STAR exceeded its {timeout}s budget")
            time.sleep(0.2)
    # Tell Popen the child is already reaped, so its own wait() does not race for an ECHILD.
    proc.returncode = os.waitstatus_to_exitcode(status)
    elapsed = time.monotonic() - started
    return proc.returncode, elapsed, usage.ru_maxrss, err_log.read_text()[-2000:]


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
    processing, _ = fill_processing(
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
    features: tuple[str, ...] | None = None,
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

    ``features`` overrides the compiler's default, and exists for exactly one job: the **cost arm**
    of the all-5-vs-pair measurement. It cannot be used to make the gate pass — the assertion below
    demands ``{Gene, GeneFull} ⊆ composed``, so any override that would hide the intron defect fails
    the gate instead of quietly narrowing it. Leave it ``None`` and the compiler decides, which is
    what the gate is *for*.
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

    processing, _ = fill_processing(
        spec=spec,
        dataset=manifest,
        processing=ProcessingInputs(
            assembly=assets.assembly,
            annotation_name=assets.annotation,
            features=features,
        ),
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
    cost: dict[str, object] = {}
    run_starsolo(
        assets,
        cdna_fq=cdna_fq,
        barcode_fq=bc_fq,
        whitelist=wl_path,
        solo=solo,
        outdir=outdir,
        threads=threads,
        cost=cost,
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
        # What this arm cost. Both arms load the same genome index, so that fixed floor sits in
        # BOTH numbers and biases the all-5/pair RATIO toward 1.0 — i.e. toward keeping Velocyto,
        # the thing we already chose. Read `star_wall_s` as a floor-inclusive ratio and take the
        # marginal difference as the honest figure.
        "cost": cost,
        "star": star_stats(outdir),
        "gene_verdict": v_gene,
        "genefull_verdict": v_full,
    }


def run_cost_sweep(
    assets: E2EAssets,
    *,
    workdir: Path,
    whitelist: Path,
    sweep: tuple[int, ...] = (2_000_000, 8_000_000, 32_000_000),
    n_cells: int = 5_000,
    intron_frac: float = 0.4,
    read_len: int = 90,
    max_genes: int = 2_000,
    threads: int = 16,
    seed: int = 0,
    features: tuple[str, ...] | None = None,
    timeout: int = 24 * 3600,
    keep_reads: bool = False,
) -> dict[str, object]:
    """Price STARsolo's peak RSS against read depth, on the compiler's own params.

    **Why a sweep and not one big run.** A single number would be dominated by the thing that does
    not vary: the hg38 index is ~30 GB resident before a single read is parsed, and on ce11 a 500x
    increase in reads moved peak RSS by 5 MB — the measurement was almost entirely index. What
    transfers to a corpus of 10^4 datasets of *different depths* is the line, not a point: an
    intercept you pay per job and a slope you pay per read. So we fit both, and report the fit's
    residual so a reader can see whether the linear model deserved to be believed.

    The extrapolation is the deliverable and also the weakest link, which is why the result carries
    ``extrapolation_factor`` explicitly: fitting to 32 M reads and quoting 500 M is a 16x reach, and
    a reader who does not know that cannot judge the number.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        print(f"[cost] {msg}", flush=True)

    note(f"loading whitelist {whitelist}")
    barcodes = read_whitelist(whitelist)
    if len(barcodes) < n_cells:
        raise E2EUnavailable(f"whitelist has {len(barcodes)} barcodes; need >= {n_cells}")
    rng = random.Random(seed)
    cbs = rng.sample(barcodes, n_cells)
    note(f"whitelist: {len(barcodes)} barcodes; sampled {n_cells} as cells")

    note(f"building gene models from {assets.gtf}")
    models = load_cost_models(assets.fasta, assets.gtf, max_genes=max_genes, seed=seed)
    if not models:
        raise E2EUnavailable(f"no usable gene models from {assets.gtf}")
    note(f"gene models: {len(models)}")

    # The params are the compiler's, derived ONCE from a small fixture: they do not depend on read
    # depth, and re-deriving them per point would only add ways for the arms to differ. Driving the
    # real compiler (rather than hand-writing a STAR command line) is what makes this a measurement
    # of *our pipeline* instead of a measurement of STARsolo in the abstract.
    note("deriving params: resolve -> fill -> compose on a small fixture")
    solo, wl_path, decided = _compose_cost_params(
        assets,
        workdir=workdir,
        models=models,
        cbs=cbs,
        barcodes=barcodes,
        features=features,
        intron_frac=intron_frac,
        read_len=read_len,
        seed=seed,
    )
    feature_list = _feature_list(solo["soloFeatures"])
    note(f"compiler decided {decided!r}; soloFeatures = {feature_list}")

    # Resume (R7: disk is state, context is cache). A preemptible partition can requeue this job at
    # any moment, and a requeue that redid five hours of STAR would make the cheap partition the
    # expensive one. A point already measured at this depth, under these same features, is a fact —
    # so it is reloaded rather than recomputed. The features check is what makes that safe: the same
    # depth measured under a different --quantify is a different measurement wearing the same tag.
    partial_path = workdir / "cost_sweep.partial.json"
    done = _load_resumable_points(partial_path, feature_list)
    if done:
        note(f"resuming: depths {sorted(done)} already measured")

    points: list[dict[str, object]] = []
    for n_reads in sweep:
        tag = f"{n_reads // 1_000_000}M"
        if n_reads in done:
            points.append(done[n_reads])
            note(f"{tag}: already measured ({done[n_reads].get('star_peak_rss_gb')} GB) — skipping")
            continue
        note(f"--- point {tag}: generating reads ---")
        cdna_fq = workdir / f"cost_{tag}_R2.fastq.gz"
        bc_fq = workdir / f"cost_{tag}_R1.fastq.gz"
        t0 = time.monotonic()
        gen = write_cost_fastqs(
            models,
            n_reads=n_reads,
            cbs=cbs,
            cdna_path=cdna_fq,
            bc_path=bc_fq,
            intron_frac=intron_frac,
            read_len=read_len,
            seed=seed,
        )
        note(f"{tag}: generated in {time.monotonic() - t0:.0f}s")

        outdir = workdir / f"star_{tag}"
        cost: dict[str, object] = {}
        note(f"{tag}: running STAR ({threads} threads)")
        # The sweep ascends, and a point can fail for the very reason we are measuring: the biggest
        # depth is the one that can exhaust the cgroup. Losing it must not lose the points below it,
        # which already determine the slope — so a failure is recorded and the sweep moves on.
        try:
            run_starsolo(
                assets,
                cdna_fq=cdna_fq,
                barcode_fq=bc_fq,
                whitelist=wl_path,
                solo=solo,
                outdir=outdir,
                threads=threads,
                cost=cost,
                timeout=timeout,
            )
        except E2EUnavailable as exc:
            note(f"{tag}: FAILED — {exc}")
            points.append({"n_reads": n_reads, **gen, "failed": True, "error": str(exc)})
        else:
            points.append({"n_reads": n_reads, **gen, **cost, "star": star_stats(outdir)})
            note(f"{tag}: peak RSS {cost.get('star_peak_rss_gb')} GB in {cost.get('star_wall_s')}s")
        if not keep_reads:
            for p in (cdna_fq, bc_fq):
                p.unlink(missing_ok=True)
            note(f"{tag}: reads deleted (--keep-reads to retain)")
        # Disk is state (R7): a hard kill (Slurm OOM, wall-clock) must not cost us the points we
        # already paid for, so every point is durable the moment it exists rather than at the end.
        (workdir / "cost_sweep.partial.json").write_text(
            json.dumps({"points": points, "soloFeatures": feature_list}, indent=2, default=str)
        )

    measured = [p for p in points if not p.get("failed")]
    fit = _fit_line([(int(p["n_reads"]), float(p["star_peak_rss_gb"])) for p in measured])  # type: ignore[arg-type]
    return {
        "assembly": assets.assembly,
        "annotation": assets.annotation,
        "star_version": _star_version(assets.star_bin),
        "decided_technology": decided,
        "soloFeatures": feature_list,
        "n_cells": n_cells,
        "n_gene_models": len(models),
        "whitelist_entries": len(barcodes),
        "intron_frac": intron_frac,
        "read_len": read_len,
        "threads": threads,
        "points": points,
        "fit": fit,
        "log": log,
    }


def _compose_cost_params(
    assets: E2EAssets,
    *,
    workdir: Path,
    models: list[GeneModel],
    cbs: list[str],
    barcodes: list[str],
    features: tuple[str, ...] | None,
    intron_frac: float,
    read_len: int,
    seed: int,
    n_probe_reads: int = 200_000,
) -> tuple[dict[str, object], Path, str | None]:
    """Drive resolve -> fill -> compose on a small fixture and return the params STAR should run.

    The fixture is small because the probe is budgeted (R3) and none of resolve/fill/compose cares
    how deep the library is — only the aligner does. Deriving the params from data rather than typing
    them here is what keeps this honest: if the compiler stopped recognizing 10x v3, or stopped
    emitting Velocyto, this reports that instead of quietly measuring a command line we wrote by hand.

    The technology is **reported, not asserted**. v3 and v3.1 are byte-identical and declared
    processing-equivalent, so the resolver is free to pick either; demanding one would be asserting
    something the KB explicitly says is not decidable, and it makes no difference to memory.
    """
    probe_cdna = workdir / "params_probe_R2.fastq.gz"
    probe_bc = workdir / "params_probe_R1.fastq.gz"
    write_cost_fastqs(
        models,
        n_reads=n_probe_reads,
        cbs=cbs,
        cdna_path=probe_cdna,
        bc_path=probe_bc,
        intron_frac=intron_frac,
        read_len=read_len,
        seed=seed,
    )
    spec = load_spec(E2E_TECH)
    registry = OnlistRegistry(offline=True)
    registry.register_synthetic("3M-february-2018", barcodes)
    resolved = resolve_dataset(
        [probe_bc, probe_cdna], registry=registry, workspace=workdir, use_cache=False
    )
    if not resolved.result.candidates:
        raise E2EUnavailable("the resolver found no candidate technology for the cost fixture")
    decided = resolved.result.candidates[0].technology
    observations = [probe_file(p) for p in (probe_bc, probe_cdna)]
    manifest = fill_manifest(
        result=resolved.result,
        spec=spec,
        observations=observations,
        registry=registry,
        experiment=ExperimentInputs(
            organism_taxid=9606,  # H. sapiens — the whole point of this arm
            samples=[
                SampleGroup(sample_id="cost", file_uris=[p.name for p in (probe_bc, probe_cdna)])
            ],
        ),
        seqforge_version=__version__,
    )
    report = validate_manifest(manifest)
    if not report.ok:
        raise E2EUnavailable(
            f"cost fixture manifest is invalid: {[b.code for b in report.blockers]}"
        )
    processing, _ = fill_processing(
        spec=spec,
        dataset=manifest,
        processing=ProcessingInputs(
            assembly=assets.assembly, annotation_name=assets.annotation, features=features
        ),
        seqforge_version=__version__,
    )
    composed = compose_plan(manifest, processing, registry=registry)
    solo = dict(composed.config["solo"])  # type: ignore[arg-type]

    # run_starsolo takes the whitelist as a path of its own (the composed value is a run-dir-relative
    # name), exactly as the gates do it.
    wl_path = workdir / "whitelist.txt"
    lines = next(iter(composed.onlist_files.values()), None)
    if lines is None:
        raise E2EUnavailable("compose materialized no onlist, so --soloCBwhitelist has no file")
    wl_path.write_text("\n".join(lines) + "\n")
    for p in (probe_cdna, probe_bc):
        p.unlink(missing_ok=True)
    return solo, wl_path, decided


def _load_resumable_points(
    partial_path: Path, feature_list: list[str]
) -> dict[int, dict[str, object]]:
    """Reload depths already measured under ``feature_list``; ``{}`` if there is nothing safe to reuse.

    The features check is the whole point. A preemptible requeue must not repay for a measurement it
    already has, but the same depth measured under a different ``--quantify`` is a *different*
    measurement wearing the same tag, and splicing a Gene-only number into an all-five curve would
    corrupt the slope silently — the failure this whole arm exists to avoid. Anything unreadable is
    treated as absent: a cache is never worth a wrong answer.
    """
    if not partial_path.is_file():
        return {}
    try:
        prior = json.loads(partial_path.read_text())
        if prior.get("soloFeatures") != feature_list:
            return {}
        return {int(p["n_reads"]): p for p in prior.get("points", []) if not p.get("failed")}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def _fit_line(points: list[tuple[int, float]]) -> dict[str, object]:
    """Least-squares ``rss_gb = intercept + slope * reads``; the slope is the whole point.

    Reports ``max_residual_gb`` so the linear model is falsifiable by its own output rather than
    assumed: if the points do not sit on a line, extrapolating from them is not defensible and the
    residual is what says so.
    """
    n = len(points)
    if n < 2:
        return {"ok": False, "reason": "need >= 2 points to fit a line"}
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom == 0:
        return {"ok": False, "reason": "all points at the same read depth"}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denom
    intercept = mean_y - slope * mean_x
    residuals = [abs(y - (intercept + slope * x)) for x, y in points]
    max_reads = max(x for x, _ in points)
    return {
        "ok": True,
        "intercept_gb": round(intercept, 3),
        "bytes_per_read": round(slope * 1024**3, 1),
        "gb_per_100m_reads": round(slope * 100e6, 3),
        "max_residual_gb": round(max(residuals), 3),
        "max_measured_reads": max_reads,
        "projected": {
            f"{d // 1_000_000}M_reads": {
                "peak_rss_gb": round(intercept + slope * d, 1),
                "extrapolation_factor": round(d / max_reads, 1),
            }
            for d in (100_000_000, 250_000_000, 500_000_000, 1_000_000_000)
        },
    }


def _star_version(star_bin: str) -> str:
    try:
        return subprocess.run(
            [star_bin, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except Exception:  # pragma: no cover - a version string is never worth failing a run for
        return "unknown"


def load_cost_models(
    fasta: Path,
    gtf: Path,
    *,
    min_len: int = 600,
    min_intron: int = 300,
    max_genes: int = 2000,
    seed: int = 0,
) -> list[GeneModel]:
    """Gene models for the COST probe: mRNA + introns, with none of the ambiguity screening.

    :func:`load_gene_models` spends nearly all of its time proving each intron is unambiguously one
    gene's, because the intron *gate* asserts an exact count and a read STAR could legitimately place
    elsewhere would make the injected truth a fiction. The cost probe asserts nothing about counts —
    it measures bytes — so that screening buys it nothing and costs a genome-wide overlap scan whose
    inner loop rebuilds an O(n) list per candidate (fine on ce11, not on hg38).

    What the cost probe does need is reads landing in **both** exons and introns, so that all five
    counting rules — Velocyto above all, which is the one being priced — have real work to do.
    """
    chroms = read_fasta(fasta)
    exons = _parse_exons(gtf)
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
            seq = chroms[chrom][istart - 1 : iend].upper()
            if "N" in seq:
                continue
            introns.append(_revcomp(seq) if strand == "-" else seq)
        if introns:
            models.append(GeneModel(gene_id=gene_id, mrna=mrna.upper(), introns=tuple(introns)))
    rng = random.Random(seed)
    rng.shuffle(models)
    return models[:max_genes]


#: All 4096 six-mers. A 12 bp UMI is two of them, so ``T[getrandbits(12)] + T[getrandbits(12)]``
#: draws uniformly from the whole 4^12 space with two list lookups instead of twelve rng.choice
#: calls. At 10^8 reads that difference is the run.
_SIXMERS: list[str] = ["".join(p) for p in _product("ACGT", repeat=6)]


def write_cost_fastqs(
    models: list[GeneModel],
    *,
    n_reads: int,
    cbs: list[str],
    cdna_path: Path,
    bc_path: Path,
    intron_frac: float = 0.4,
    read_len: int = 90,
    seed: int = 0,
    chunk: int = 200_000,
) -> dict[str, int]:
    """Emit ``n_reads`` 10x-3'-v3-shaped reads from pre-mRNA. No ground truth — this prices, not proves.

    Two deliberate departures from :func:`simulate_nuclei`, both of which exist *because* it is a
    cost arm and not a gate:

    **UMIs are drawn at random, not forced globally unique.** The gate's uniqueness trick makes the
    injected count exactly the read count, and it is also a hard ceiling: a 12 bp UMI has 4^12 =
    16 777 216 values, so a run that demands a distinct one per read cannot exceed ~16.8 M reads and
    the rejection sampling degrades long before that. A cost probe must reach corpus scale, and it
    asserts no counts, so the constraint is pure cost. Drawing at random instead means near-zero
    duplication, i.e. **more distinct UMIs than real data of the same depth** — which makes every
    number here an over-estimate of the solo structures, and an over-estimate is the right side to
    err on when the output is a memory request.

    **Barcodes come from the real whitelist**, so STARsolo does the CB matching it would really do.
    """
    exon_src: list[tuple[str, int, int]] = []  # (seq, lo, hi) — 3'-biased window, as a 3' kit is
    intron_src: list[tuple[str, int]] = []  # (seq, hi)
    for model in models:
        mrna = model.mrna
        if len(mrna) >= read_len:
            tail = min(len(mrna), 500)
            exon_src.append((mrna, len(mrna) - tail, len(mrna) - read_len))
        for intron in model.introns:
            if len(intron) >= read_len:
                intron_src.append((intron, len(intron) - read_len))
    if not exon_src or not intron_src:
        raise E2EUnavailable(
            "the cost fixture needs both exonic and intronic source sequence; got "
            f"{len(exon_src)} exon and {len(intron_src)} intron sources"
        )

    rng = random.Random(seed)
    randrange, getrandbits, random_f = rng.randrange, rng.getrandbits, rng.random
    sixmers, n_cb = _SIXMERS, len(cbs)
    n_ex, n_in = len(exon_src), len(intron_src)
    cdna_qual, bc_qual = "I" * read_len, "I" * (len(cbs[0]) + 12)
    n_intronic = 0

    with (
        gzip.open(cdna_path, "wt", compresslevel=1) as cdna_fh,
        gzip.open(bc_path, "wt", compresslevel=1) as bc_fh,
    ):
        written = 0
        while written < n_reads:
            n = min(chunk, n_reads - written)
            cdna_buf: list[str] = []
            bc_buf: list[str] = []
            for i in range(written, written + n):
                if random_f() < intron_frac:
                    seq, hi = intron_src[randrange(n_in)]
                    start = randrange(0, hi + 1)
                    n_intronic += 1
                else:
                    seq, lo, hi = exon_src[randrange(n_ex)]
                    start = randrange(lo, hi + 1)
                cdna_buf.append(f"@SIM{i}\n{seq[start : start + read_len]}\n+\n{cdna_qual}\n")
                umi = sixmers[getrandbits(12)] + sixmers[getrandbits(12)]
                bc_buf.append(f"@SIM{i}\n{cbs[randrange(n_cb)]}{umi}\n+\n{bc_qual}\n")
            cdna_fh.write("".join(cdna_buf))
            bc_fh.write("".join(bc_buf))
            written += n
    return {"n_reads": n_reads, "n_intronic": n_intronic, "n_exonic": n_reads - n_intronic}


def read_whitelist(path: Path) -> list[str]:
    """Load a 10x whitelist (plain or gzipped) as barcode strings."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as fh:  # type: ignore[operator]
        return [ln.strip() for ln in fh if ln.strip()]


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
