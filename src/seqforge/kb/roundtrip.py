"""``kb roundtrip`` — the self-test: spec -> synth FASTQ -> probe -> recover; assert == declared.

Generic over any spec: for every declared read it checks that the probe recovers the declared fixed
length, that barcode windows recur (low distinct-ratio), and that UMI windows are ~unique (high
distinct-ratio). Uses a temp directory; touches no real data.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ..probe import probe_file
from ..probe.signals import window_distinct_ratio
from .anchor import resolve_windows
from .generate import generate_reads, write_fastq_gz
from .loader import load_spec
from .schema import Read


def _write_fastq_gz(path: Path, seqs: list[str]) -> None:
    write_fastq_gz(path, seqs)


def _anchored_distinct_ratio(seqs: list[str], read: Read, element_name: str) -> float | None:
    """``distinct/total`` of a FLOATING element, sliced at each read's phase-detected frame.

    A fixed element has a constant window :func:`window_distinct_ratio` can slice; an anchored one does
    not (the diversity insert staggers it per read), so recover its frame first. This is what makes the
    round-trip actually verify anchoring: if the resolver could not recover the staggered cell-label
    blocks, their slices would not recur and ``barcode_recurs`` would go red.
    """
    slices = [
        s[a:b]
        for s in seqs
        if (w := resolve_windows(s, read)) is not None and element_name in w
        for a, b in (w[element_name],)
        if s[a:b]
    ]
    if not slices:
        return None
    return len(set(slices)) / len(slices)


def run_roundtrip(tech_id: str, *, n: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Round-trip one technology and return ``{tech, passed, checks:[...]}``."""
    spec = load_spec(tech_id)
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
            for el in read.elements:
                if el.start is not None and el.end is not None:
                    ratio = window_distinct_ratio(seqs, el.start, el.end)
                elif el.anchor is not None:
                    # a floating element: recover its per-read frame rather than skipping it (pre-#43
                    # every anchored element was skipped, so the round-trip proved nothing about them).
                    ratio = _anchored_distinct_ratio(seqs, read, el.name)
                else:
                    continue  # a variable-length insert with no anchor (the VB itself): no window to
                    # check — its recovery is proven by the downstream anchored elements resolving.
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

    return {"tech": tech_id, "passed": all(c["ok"] for c in checks), "checks": checks}
