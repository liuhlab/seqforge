"""``kb roundtrip`` — the self-test: spec -> synth FASTQ -> probe -> recover; assert == declared.

Generic over any spec: for every declared read it checks that the probe recovers the declared fixed
length, that a `linker`/`fixed` element's window comes back carrying the very sequence the spec
declares, that barcode windows recur (low distinct-ratio), and that UMI windows are ~unique (high
distinct-ratio). Uses a temp directory; touches no real data.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..probe import probe_file
from ..probe.signals import distinct_ratio, modal_consensus, window_bases
from .anchor import element_bases, resolve_windows
from .generate import generate_reads, write_fastq_gz
from .loader import load_spec
from .schema import Spec


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    write_fastq_gz(path, seqs)


def run_roundtrip(tech_id: str, *, n: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Round-trip one technology and return ``{tech, passed, checks:[...]}``."""
    checks = roundtrip_checks(load_spec(tech_id), n=n, seed=seed)
    return {"tech": tech_id, "passed": all(c["ok"] for c in checks), "checks": checks}


def roundtrip_checks(spec: Spec, *, n: int = 2000, seed: int = 0) -> list[dict[str, Any]]:
    """Every check this spec's own synthetic reads support, in read then element order.

    Takes the SPEC and not its id, because a check nobody can watch fail is not a check: a caller
    hands this a deliberately broken copy of a shipped entry — a linker one base short of the
    coordinates it declares — and the constant-sequence check goes red on it. Loading the id is
    :func:`run_roundtrip`'s half, and it belongs to the verb, not to the self-test.
    """
    reads = generate_reads(spec, n=n, seed=seed)
    checks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as td:
        for read in spec.reads:
            seqs = reads[read.id]
            path = Path(td) / f"{read.id}.fastq.gz"
            _write_fastq_gz(path, seqs)
            obs = probe_file(path)

            if read.min_len is not None and read.min_len == read.max_len:
                checks.append(
                    {
                        "read": read.id,
                        "check": "length",
                        "ok": obs.read_length.mode == read.min_len,
                        "declared": read.min_len,
                        "recovered": obs.read_length.mode,
                    }
                )
            # an open-ended cDNA/gDNA read must probe back as variable-length (non-vacuous for the
            # no-barcode bulk branch, whose only structural claim is "two variable cDNA reads").
            has_open_cdna = any(
                el.type in ("cdna", "gdna") and el.end is None for el in read.elements
            )
            if has_open_cdna and read.min_len != read.max_len:
                checks.append(
                    {
                        "read": read.id,
                        "check": "cdna_variable",
                        "ok": obs.read_length.n_distinct > 1,
                        "n_distinct": obs.read_length.n_distinct,
                    }
                )
            frames: list[dict[str, tuple[int, int]] | None] | None = None
            for el in read.elements:
                if el.start is not None and el.end is not None:
                    bases = window_bases(seqs, el.start, el.end)
                elif el.anchor is not None:
                    # a floating element: recover its per-read frame rather than skipping it (pre-#43
                    # every anchored element was skipped, so the round-trip proved nothing about them).
                    # One resolution per read serves every element on it -- the round-trip's own small
                    # echo of `WindowProbe._frame_cache`, and the reason `element_bases` takes frames.
                    if frames is None:
                        frames = [resolve_windows(s, read) for s in seqs]
                    bases = element_bases(seqs, frames, el.name)
                else:
                    continue  # a variable-length insert with no anchor (the VB itself): no window to
                    # check — its recovery is proven by the downstream anchored elements resolving.
                if el.type in ("linker", "fixed") and el.sequence is not None:
                    # THE DECLARED SEQUENCE ITSELF, which nothing checked on any entry: the loop
                    # computed a statistic for every element and recorded a check only for
                    # onlist-backed barcodes and UMIs, so a `linker`/`fixed` fell straight through.
                    # SPLiT-seq's two 30 bp linkers — the sequences its own guide holds up as that
                    # entry's whole discipline, and where three published sources turned out to
                    # disagree with the instrument at base 8 — were among them (#285).
                    #
                    # What can fail, since the generator writes this same string: the two derivations
                    # of WHERE it goes. The generator concatenates elements in order; this reads the
                    # window back off the declared `[start, end)` (or the frame the anchored resolver
                    # recovers). A `sequence` whose length disagrees with its own coordinates, or an
                    # upstream element whose width does, shifts everything after it and lands here as
                    # a consensus that is not what the spec says. On the anchored path the claim is
                    # weaker by construction — the frame was found BY matching this linker — but a
                    # wrong width or a mis-ordered chain still shows up.
                    recovered = modal_consensus(bases)
                    checks.append(
                        {
                            "read": read.id,
                            "check": f"constant_sequence:{el.name}",
                            "ok": recovered == el.sequence,
                            "declared": el.sequence,
                            "recovered": recovered,
                        }
                    )
                    continue
                ratio = distinct_ratio(bases)
                if el.type == "barcode" and el.onlist:
                    checks.append(
                        {
                            "read": read.id,
                            "check": f"barcode_recurs:{el.name}",
                            "ok": ratio is not None and ratio < 0.5,
                            "ratio": ratio,
                        }
                    )
                elif el.type == "umi":
                    checks.append(
                        {
                            "read": read.id,
                            "check": f"umi_unique:{el.name}",
                            "ok": ratio is not None and ratio > 0.7,
                            "ratio": ratio,
                        }
                    )

    return checks
