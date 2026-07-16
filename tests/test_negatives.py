"""The day-one negatives: refusal (not a guess) is the correct answer.

1. truncated/corrupt gzip -> ``Blocker(TRUNCATED_GZIP)`` (exit 3)
2. an ONT run (technology absent from the KB) -> ``Blocker(UNSUPPORTED_TECHNOLOGY)`` (exit 3), never
   a silent guess
3. metadata says v2 but the reads say v3 -> a surfaced ``Conflict`` (26 bp asserted vs 28 bp
   observed) (exit 4), never a silent pick
4. a pre-trimmed technical read -> ``Blocker(PRETRIMMED_VARIABLE_LENGTH)`` (exit 3). The quiet one:
   1-3 are all loud, and this one scores like a clean dataset.
"""

from __future__ import annotations

import gzip
import random
from pathlib import Path

from seqforge import kb
from seqforge.io import OnlistRegistry
from seqforge.models.blocker import BlockerCode
from seqforge.resolve import Hypothesis, resolve_dataset


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


def test_truncated_gzip_blocks(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])
    # cut R1's gzip mid-stream: valid records then an abrupt end -> truncated (not merely corrupt)
    data = f1.read_bytes()
    f1.write_bytes(data[: int(len(data) * 0.6)])

    out = resolve_dataset([f1, f2], registry=_registry_for(spec), use_cache=False)
    assert out.exit_code() == 3
    assert not out.result.candidates
    codes = {b.code for b in out.result.blockers}
    assert BlockerCode.TRUNCATED_GZIP in codes
    blk = next(b for b in out.result.blockers if b.code == BlockerCode.TRUNCATED_GZIP)
    assert blk.remedy  # actionable, non-empty


def test_a_pretrimmed_technical_read_blocks(tmp_path: Path) -> None:
    """The quiet negative: it scores like a clean dataset, so nothing else catches it.

    `read_length_compatible` gates on the read-length **mode**, so a barcode read that is mostly
    28 bp with a trimmed tail passes every geometry check and wins its candidate outright. Downstream
    never looks again: STARsolo reads the barcode from a fixed offset, and on a shifted read that
    offset is an arbitrary 16-mer — it matches no whitelist, the cell is dropped, the matrix is thin,
    and STAR exits 0. That is the silent-garbage path §5 was written to close, and
    `PRETRIMMED_VARIABLE_LENGTH` sat declared-but-never-emitted while it stayed open.

    Note only R1 is trimmed here. R2 is cDNA — open-ended and *legitimately* variable — which is
    exactly why this cannot be "variable length is bad": it has to be variable length on a read the
    chemistry declares fixed.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    # cutadapt ran over the barcode read: most reads survive at 28 bp, a minority come back short.
    trimmed = [s[:20] if i % 20 == 0 else s for i, s in enumerate(reads["R1"])]
    assert len({len(s) for s in trimmed}) == 2  # the fixture really is variable...
    assert max(set(trimmed), key=len).__len__() == 28  # ...with the mode still at the declared 28

    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    _write_fastq_gz(f1, trimmed)
    _write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset([f1, f2], registry=_registry_for(spec), use_cache=False)

    assert out.exit_code() == 3
    assert not out.result.candidates  # refused: no manifest may be filled over this
    blk = next(b for b in out.result.blockers if b.code == BlockerCode.PRETRIMMED_VARIABLE_LENGTH)
    assert blk.subject.ref == "sample_R1.fastq.gz"  # names the trimmed file, not the clean cDNA
    assert "sra-pub-src" in blk.remedy  # actionable: where the untrimmed original lives


def test_an_untrimmed_dataset_does_not_trip_the_pretrimmed_blocker(tmp_path: Path) -> None:
    """The other half: cDNA is variable by design, and must not be mistaken for a trimmer's work.

    A guard that fired on every 10x dataset would be deleted within a day.
    """
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=3000, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"
    f2 = tmp_path / "sample_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])  # open-ended cDNA: genuinely many distinct lengths

    out = resolve_dataset([f1, f2], registry=_registry_for(spec), use_cache=False)

    codes = {b.code for b in out.result.blockers}
    assert BlockerCode.PRETRIMMED_VARIABLE_LENGTH not in codes
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"


def test_ont_unsupported_technology_is_refused_not_guessed(tmp_path: Path) -> None:
    # A single long-read ONT file: no KB technology's read set can be satisfied -> refuse, don't guess.
    rng = random.Random(0)
    long_reads = [
        "".join(rng.choice("ACGT") for _ in range(rng.randint(500, 3000))) for _ in range(200)
    ]
    f = tmp_path / "ont_run.fastq.gz"
    _write_fastq_gz(f, long_reads)

    out = resolve_dataset([f], use_cache=False)
    assert out.exit_code() == 3
    assert not out.result.candidates
    codes = {b.code for b in out.result.blockers}
    assert codes == {BlockerCode.UNSUPPORTED_TECHNOLOGY}


def test_metadata_v2_vs_reads_v3_surfaces_conflict(tmp_path: Path) -> None:
    spec = kb.load_spec("10x-3p-gex-v3")
    reads = kb.generate_reads(spec, n=1500, seed=0)
    f1 = tmp_path / "sample_R1.fastq.gz"  # observed 28 bp
    f2 = tmp_path / "sample_R2.fastq.gz"
    _write_fastq_gz(f1, reads["R1"])
    _write_fastq_gz(f2, reads["R2"])

    out = resolve_dataset(
        [f1, f2],
        registry=_registry_for(spec),
        hypothesis=Hypothesis(value="10x-3p-gex-v2", id="meta-1", confidence=0.9),
        use_cache=False,
    )
    # the library takes the observed chemistry (v3), but the disagreement is SURFACED, not silent
    assert out.result.candidates[0].technology == "10x-3p-gex-v3"
    assert out.exit_code() == 4
    assert len(out.result.conflicts) == 1
    conflict = out.result.conflicts[0]
    assert conflict.kind == "observed_vs_asserted"
    assert conflict.status == "open"
    values = {p.value: p.basis for p in conflict.positions}
    assert values == {"26": "asserted", "28": "observed"}
