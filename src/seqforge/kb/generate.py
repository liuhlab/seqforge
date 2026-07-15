"""Synthetic FASTQ generation derived PURELY from ``spec.reads`` (R10 round-trip).

The generator never reads ``signature`` or ``backend`` — that is what makes the round-trip
(``spec -> synth -> probe -> recover; assert recovered == declared``) a real test rather than a
tautology. Barcodes with an onlist are drawn from a fixed synthetic cell pool reused across reads so
the recurrence signal (low distinct-ratio) is realistic; UMIs are fresh-random (high distinct-ratio).
"""

from __future__ import annotations

import gzip
import random
from pathlib import Path

from .schema import Element, Spec

_BASES = "ACGT"


def write_fastq_gz(path: Path, seqs: list[str], *, prefix: str = "SIM") -> None:
    """Write a REPRODUCIBLE ``.fastq.gz``: same reads in, same bytes out, hence the same sha256.

    ``gzip.open(path, "wt")`` stamps the current mtime into the gzip header (and embeds the source
    filename), so regenerating identical reads a second later produces different bytes. Everything
    downstream is content-addressed by file sha256 (R7), so a wall-clock-dependent header silently
    changes the dataset id — two runs over the same synthetic input never share a cache entry, and
    "deterministic in (spec, seed)" quietly stops being true at the byte level where it is claimed.
    ``mtime=0`` + ``filename=""`` make the output a pure function of the reads.

    One writer, because this was duplicated at three sites and each carried the same latent bug.
    """
    payload = "".join(
        f"@{prefix}:{i}\n{s}\n+\n{'I' * len(s)}\n" for i, s in enumerate(seqs)
    ).encode()
    with open(path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        gz.write(payload)


def _rand(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(_BASES) for _ in range(n))


def _fixed_length(el: Element) -> int | None:
    if el.start is not None and el.end is not None:
        return el.end - el.start
    if el.min_len is not None:
        return el.min_len
    return None


def build_pools(spec: Spec, *, seed: int = 0, pool_size: int = 64) -> dict[str, list[str]]:
    """Build the synthetic barcode pool backing each onlist *alias*, keyed by ``Element.onlist``.

    Deterministic in ``(seed, pool_size)`` and independent of read generation, so a test can
    reconstruct the exact whitelist the reads will be drawn from (to register it as a synthetic
    onlist for the resolver). ``pool_size`` distinct barcodes drive the low-distinct-ratio recurrence
    signal; keep it well below the read count.
    """
    rng = random.Random(seed)
    pools: dict[str, list[str]] = {}
    for read in spec.reads:
        for el in read.elements:
            if el.onlist and el.onlist not in pools:
                length = _fixed_length(el) or 16
                pools[el.onlist] = [_rand(rng, length) for _ in range(pool_size)]
    return pools


def generate_reads(
    spec: Spec,
    *,
    n: int = 2000,
    seed: int = 0,
    pool_size: int = 64,
    cdna_min: int = 60,
    cdna_max: int = 91,
    pools: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Generate ``n`` synthetic reads per declared read, keyed by ``Read.id``.

    ``pool_size`` sets how many distinct barcodes back each onlist (drives the recurrence signal);
    keep it well below ``n`` so the cell-barcode distinct-ratio lands in-band. Pass ``pools`` (from
    :func:`build_pools` with the same ``seed``/``pool_size``) to reuse a known whitelist.
    """
    if pools is None:
        pools = build_pools(spec, seed=seed, pool_size=pool_size)
    rng = random.Random(seed + 1)  # a stream distinct from pool construction, still deterministic
    result: dict[str, list[str]] = {}
    for read in spec.reads:
        seqs: list[str] = []
        for _ in range(n):
            seqs.append(
                "".join(_gen_element(el, rng, pools, cdna_min, cdna_max) for el in read.elements)
            )
        result[read.id] = seqs
    return result


def _gen_element(
    el: Element,
    rng: random.Random,
    pools: dict[str, list[str]],
    cdna_min: int,
    cdna_max: int,
) -> str:
    if el.type in ("linker", "fixed"):
        return el.sequence or ""
    length = _fixed_length(el)
    if el.type == "barcode":
        return rng.choice(pools[el.onlist]) if el.onlist else _rand(rng, length or 8)
    if el.type == "umi":
        return _rand(rng, length or 10)
    if el.type in ("cdna", "gdna"):
        return _rand(rng, length or rng.randint(cdna_min, cdna_max))
    if el.type == "poly_t":
        return "T" * (length or 10)
    if el.type == "poly_a":
        return "A" * (length or 10)
    if el.type == "index":
        return _rand(rng, length or 8)
    return _rand(rng, length or 1)
