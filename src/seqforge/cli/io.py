"""`seqforge io` -- the network + onlist surface: peek, accession resolution, records, vocabularies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import typer

from ..io import DEFAULT_REGISTRY, HF_BENCHMARK_REPO, Orientation, default_registry
from ..io.remote import NotYetImplemented, peek, resolve_accession
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from ..workspace import records_dir
from ._common import _today
from .root import io_app, onlist_app


@onlist_app.command("list")
def io_onlist_list() -> None:
    """List the onlists in the default registry. Shipped ones need no network and no setup."""
    rows = []
    for name in DEFAULT_REGISTRY.names():
        entry = DEFAULT_REGISTRY.get(name)
        rows.append(
            {
                "name": entry.name,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "shipped": entry.shipped,
                "fetchable": entry.fetchable,
            }
        )
    typer.echo(json.dumps({"onlists": rows}, indent=2))


@onlist_app.command("show")
def io_onlist_show(
    name: str = typer.Argument(..., help="Registry name, e.g. 3M-february-2018."),
) -> None:
    """Show one onlist registry entry as JSON."""
    if not DEFAULT_REGISTRY.has(name):
        typer.echo(f"unknown onlist {name!r}", err=True)
        raise typer.Exit(2)
    entry = DEFAULT_REGISTRY.get(name)
    typer.echo(
        json.dumps(
            {
                "name": entry.name,
                "uri": entry.uri,
                "sha256": entry.sha256,
                "width": entry.width,
                "orientation": entry.orientation,
                "n_entries": entry.n_entries,
                "shipped": entry.shipped,
                "fetchable": entry.fetchable,
                "source_sha256": entry.source_sha256,
            },
            indent=2,
        )
    )


@onlist_app.command("write")
def io_onlist_write(
    name: str = typer.Argument(..., help="Registry name, e.g. 3M-february-2018."),
    out: Path = typer.Option(..., "--out", "-o", help="Where to write the barcode text."),
    onlist_dir: Path | None = typer.Option(
        None, "--onlist-dir", help="A directory of already-downloaded whitelists."
    ),
) -> None:
    """Materialize a whitelist as the text STARsolo reads. Called BY the composed Snakefile.

    This is the verb behind `rule onlist`, and the reason that rule exists. 10x's v3 whitelist is
    6 794 880 barcodes = 111 MB of text, and `compose` used to write it into every run directory --
    so one dataset compiled three ways cost a third of a gigabyte of identical bytes, permanently,
    for a file STAR opens once. Now the pipeline builds it, uses it, and `temp()` deletes it.

    The shipped form is 522 kB of packed deltas; this is the expansion. Nothing is fetched when the
    list ships with the package, which is the case for every 10x whitelist.
    """
    from ..io.onlist import OnlistNotAvailable, write_onlist_text

    registry = (
        default_registry(offline=False, local_dir=onlist_dir) if onlist_dir else DEFAULT_REGISTRY
    )
    try:
        n = write_onlist_text(registry, name, out)
    except OnlistNotAvailable as exc:
        typer.echo(
            json.dumps({"error": "onlist_unavailable", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"onlist": name, "path": str(out), "n_entries": n}, indent=2))


@onlist_app.command("pack")
def io_onlist_pack(
    text: Path = typer.Argument(..., help="The whitelist as text (.txt or .txt.gz)."),
    name: str = typer.Option(..., "--name", help="Registry name, e.g. 3M-february-2018."),
    uri: str = typer.Option(
        "", "--uri", help="Where this list came from, recorded for provenance."
    ),
    # Typed as the vocabulary, so typer offers the three values in `--help` and refuses a fourth at
    # exit 2 before the body runs — the value can no longer reach `index.json`, where nothing else
    # would have questioned it.
    orientation: Orientation = typer.Option(
        "forward", "--orientation", help="Which strand the barcodes are written on."
    ),
) -> None:
    """**Maintenance verb.** Pack a whitelist into the shipped form and record it in the index.

    This is how a new barcode list joins the package: `pack` it, commit the `.codes.gz` and the
    updated `index.json`, done. Nothing else to remember and nothing to hand-edit -- this verb is the
    only writer of `index.json`, which is what stops the index drifting from the blobs beside it.

    The shipped form is 2-bit-packed, sorted, de-duplicated, delta-encoded and gzipped: 10x's
    6 794 880-barcode v3 list is 522 kB here against 12 MB as their `.txt.gz`. That is why shipping
    them is cheap, and it also removes the need for a `.npy` precompilation step -- nothing re-packs
    6.8M barcodes per process any more.
    """
    import gzip as _gzip
    import hashlib as _hashlib

    from ..io.onlist import PackedOnlist, write_shipped

    raw = text.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = _gzip.decompress(raw)
    source_sha = _hashlib.sha256(raw).hexdigest()
    barcodes = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    if not barcodes:
        typer.echo(f"{text} contains no barcodes", err=True)
        raise typer.Exit(2)
    packed = PackedOnlist.from_barcodes(barcodes)
    blob = write_shipped(
        name,
        packed.codes,
        width=packed.width,
        uri=uri,
        orientation=orientation,
        source_sha256=source_sha,
    )
    typer.echo(
        json.dumps(
            {
                "name": name,
                "packed": str(blob),
                "bytes": blob.stat().st_size,
                "n_entries": packed.n_entries,
                "width": packed.width,
                "source_sha256": source_sha,
            },
            indent=2,
        )
    )


@io_app.command("h5ad")
def io_h5ad(
    solo_dir: Path = typer.Option(..., "--solo-dir", help="A STARsolo `Solo.out` directory."),
    features: str = typer.Option(
        ..., "--features", help="The run's --soloFeatures, space-separated (e.g. 'Gene GeneFull')."
    ),
    primary: str = typer.Option(
        ..., "--primary", help="Which feature becomes X (the rest become layers)."
    ),
    out_prefix: Path = typer.Option(
        ..., "--out-prefix", help="Output path prefix; '.h5ad' / '.velocyto.h5ad' are appended."
    ),
) -> None:
    """Package a Solo.out's raw matrices as .h5ad — the last step of the composed pipeline.

    Called by `starsolo.smk`'s `solo_to_h5ad` rule, which is why it is a verb and not a `run:` block:
    a `shell:` is rendered by `snakemake -n -p`, so compose's wiring gate can see it.

    Exit 3 on a Blocker-shaped refusal — the axes of the features being stacked disagree, or a matrix
    STAR was supposed to write is absent.
    """
    from ..models.processing import SoloFeature
    from ..workflows.h5ad import SOLO_FEATURE_OUTPUT, H5adError, write_h5ad

    requested = features.split()
    unknown = [f for f in [*requested, primary] if f not in SOLO_FEATURE_OUTPUT]
    if unknown:
        typer.echo(
            json.dumps({"error": f"unknown --soloFeatures value(s): {sorted(set(unknown))}"}),
            err=True,
        )
        raise typer.Exit(2)
    try:
        written = write_h5ad(
            solo_dir,
            cast(list[SoloFeature], requested),
            cast(SoloFeature, primary),
            out_prefix,
        )
    except H5adError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": [str(p) for p in written]}, indent=2))


@io_app.command("qc-bundle")
def io_qc_bundle(
    solo_dir: Path = typer.Option(..., "--solo-dir", help="A STARsolo `Solo.out` directory."),
    run_dir: Path = typer.Option(
        ..., "--run-dir", help="The sample directory holding STAR's Log.*.out / SJ.out.tab."
    ),
    features: str = typer.Option(
        ..., "--features", help="The run's --soloFeatures, space-separated."
    ),
    sample: str = typer.Option(..., "--sample", help="Sample id, recorded in the bundle."),
    out: Path = typer.Option(..., "--out", help="Output path for the gzipped JSON bundle."),
    assembly: str | None = typer.Option(
        None, "--assembly", help="UCSC assembly id, recorded for CRAM-reference provenance."
    ),
) -> None:
    """Bundle STARsolo's stats + run logs into one gzipped JSON — a finalize step of the pipeline.

    Called by `starsolo.smk`'s `qc_bundle` rule (a `shell:`, so compose's wiring gate sees it). Exit 3
    if a file STAR was supposed to write is missing.
    """
    from ..models.processing import SoloFeature
    from ..workflows.h5ad import SOLO_FEATURE_OUTPUT
    from ..workflows.qc import QcError, write_qc_bundle

    requested = features.split()
    unknown = [f for f in requested if f not in SOLO_FEATURE_OUTPUT]
    if unknown:
        typer.echo(
            json.dumps({"error": f"unknown --soloFeatures value(s): {sorted(set(unknown))}"}),
            err=True,
        )
        raise typer.Exit(2)
    try:
        written = write_qc_bundle(
            solo_dir,
            run_dir,
            cast(list[SoloFeature], requested),
            out,
            sample=sample,
            assembly=assembly,
        )
    except QcError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("fragments")
def io_fragments(
    raw: Path = typer.Option(..., "--raw", help="chromap's raw (unsorted) fragments file."),
    out: Path = typer.Option(
        ...,
        "--out",
        help="Output path for the bgzipped fragments.tsv.gz (its .tbi lands beside it).",
    ),
) -> None:
    """Finalize chromap's fragments into a sorted, bgzipped, tabix-indexed `fragments.tsv.gz`.

    Called by `chromap.smk`'s `fragments_finalize` rule (a `shell:`, so compose's wiring gate sees it) —
    the ATAC analog of `io h5ad`. Shells to htslib (`bgzip`/`tabix`) from the `align-dna` container. Exit
    3 if chromap's raw output is missing or htslib is unavailable.
    """
    from ..workflows.fragments import FragmentsError, write_fragments

    try:
        written = write_fragments(raw, out)
    except FragmentsError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("fragments-qc")
def io_fragments_qc(
    fragments: Path = typer.Option(
        ..., "--fragments", help="A fragments file (plain .tsv or bgzipped .tsv.gz)."
    ),
    sample: str = typer.Option(..., "--sample", help="Sample id, recorded in the summary."),
    assembly: str = typer.Option(
        ..., "--assembly", help="UCSC assembly id, recorded in the summary."
    ),
    out: Path = typer.Option(..., "--out", help="Output path for the gzipped JSON summary."),
) -> None:
    """Summarize a fragments file into one gzipped JSON — the ATAC analog of `io qc-bundle`.

    Called by `chromap.smk`'s `fragments_qc` rule. Pure Python over the fragments text (no external
    binary), so it needs no container. Exit 3 on a malformed fragments file.
    """
    from ..workflows.fragments import FragmentsError, write_fragments_qc

    try:
        written = write_fragments_qc(fragments, out, sample=sample, assembly=assembly)
    except FragmentsError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("cram")
def io_cram(
    bam: Path = typer.Option(..., "--bam", help="STAR's Aligned.sortedByCoord.out.bam."),
    assembly: str = typer.Option(..., "--assembly", help="UCSC assembly id; the CRAM reference."),
    out: Path = typer.Option(..., "--out", help="Output CRAM path ('.crai' is written beside it)."),
    threads: int = typer.Option(1, "--threads", help="samtools view/index threads."),
) -> None:
    """Encode STAR's coordinate-sorted BAM as a CRAM against the liulab-genome reference.

    Called by `starsolo.smk`'s `solo_to_cram` rule. The reference FASTA is resolved at run time from
    the assembly id via `liulab-genome` (never a baked path), exactly as `rule genome_index` resolves
    the STAR index. Exit 3 on a samtools failure or an unresolvable reference.

    **It sorts nothing, and there is no memory knob, because there is no longer a sort.** The module
    asks STAR for `BAM SortedByCoordinate` — the only output STAR will put `CB`/`UB` in — so the BAM
    arrives in order and what remains is a streaming pipe: drop the secondary alignments, rewrite each
    read name to `r<N>`, encode CRAM. Nothing here buffers, so nothing here needs a budget. That
    deleted a whole re-sort pass over every BAM, and with it the `samtools.<pid>.<tid>.tmp.*` files a
    killed sort left in the pipeline directory: they were undeclared outputs, so snakemake could not
    clean them, and 41 GiB of them had accumulated across five run directories. Passing `-T` to a
    snakemake-owned directory would have fixed that too; a step that cannot leak beats a step that
    must be configured not to.
    """
    from ..workflows.cram import CramError, bam_to_cram

    try:
        from genome import (
            Genome,  # untyped lab package; resolved here, off the strict workflow path
        )
    except ImportError as exc:  # pragma: no cover - depends on the host
        typer.echo(json.dumps({"error": f"liulab-genome is not importable: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    try:
        fasta = Path(str(Genome(assembly).fasta_path))
        written = bam_to_cram(bam, fasta, out, threads=threads)
    except CramError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("publish-package")
def io_publish_package(
    package: Path = typer.Argument(..., help="A local <dataset>.fingerprint.tar.gz to publish."),
    path_in_repo: str | None = typer.Option(
        None,
        "--path-in-repo",
        help="Destination path inside the repo (default: packages/<the file's own name>). It must "
        "equal the `hf:` key of the eval recipe that consumes it.",
    ),
    repo: str = typer.Option(
        HF_BENCHMARK_REPO, "--repo", help="Hugging Face DATASET repo id to commit to."
    ),
    revision: str = typer.Option("main", "--revision", help="Branch to commit on."),
    token: str | None = typer.Option(
        None,
        "--token",
        help="HF write token. Default: whatever `huggingface_hub` already has (HF_TOKEN, or a "
        "`hf auth login` session). Refuses rather than guessing when none is present.",
    ),
    message: str | None = typer.Option(
        None, "--message", "-m", help="Commit message (default: 'Publish <path in repo>')."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run/--no-dry-run",
        help="Resolve the destination, read the pin and hash the bytes — upload nothing, and touch "
        "no credential.",
    ),
) -> None:
    """Publish a fingerprint package to the public benchmark corpus. The producer half of `preflight`.

    `preflight` builds a package; this is what puts one in the corpus, so a dataset's route from a
    maintainer's disk to a benchmark case is a verb with an exit code rather than a remembered
    sequence of clicks. It lives under `io` and not beside `preflight` because it is a network
    operation and nothing else: `preflight` reads bytes and writes a file, while this one only talks
    to a remote archive, and the `io` group is the one place a reader is entitled to expect that.

    The pin is READ AND VALIDATED before a byte is sent — a tarball that is not a fingerprint package
    is refused here rather than becoming a corrupt package every future run of that case has to
    diagnose. `--dry-run` answers "where does this go, under what name, and what is in it" for free.

        seqforge io publish-package GSE110823.fingerprint.tar.gz --dry-run
        seqforge io publish-package GSE110823.fingerprint.tar.gz

    Exit 2 if the file is missing or is not a fingerprint package, or if a real upload has no
    credential; exit 1 if the archive refuses the commit.
    """
    from ..io.benchmark import publish_benchmark_package

    try:
        if not dry_run and token is None:
            from huggingface_hub import get_token

            token = get_token()
            if not token:
                typer.echo(
                    json.dumps(
                        {
                            "error": "no_hf_token",
                            "detail": "publishing writes to a public repo and needs a write token: "
                            "pass --token, set HF_TOKEN, or run `hf auth login`. Use --dry-run to "
                            "resolve the destination without one.",
                        },
                        indent=2,
                    ),
                    err=True,
                )
                raise typer.Exit(2)
        result = publish_benchmark_package(
            package,
            rel_path=path_in_repo,
            repo=repo,
            revision=revision,
            token=token,
            message=message,
            dry_run=dry_run,
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(json.dumps({"error": "not_a_package", "detail": str(exc)}, indent=2), err=True)
        raise typer.Exit(2) from exc
    except OSError as exc:
        typer.echo(json.dumps({"error": "publish_failed", "detail": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@io_app.command("peek")
def io_peek(
    uri: str = typer.Argument(..., help="Remote FASTQ URI to range-read."),
    max_reads: int = typer.Option(4, help="Records to report from the fetched prefix."),
    max_bytes: int = typer.Option(1 << 16, help="Compressed bytes to range-read (budget)."),
) -> None:
    """Range-read the head of a remote gzipped FASTQ. Never downloads the file.

    64 KB is ~0.013% of a 517 MB run. Exit 1 if the host ignores Range and answers 200 with the whole
    file — bounded means bounded by the server, not by our intentions.
    """
    from ..io.remote import RemoteError

    try:
        typer.echo(json.dumps(peek(uri, max_reads=max_reads, max_bytes=max_bytes), indent=2))
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc


@io_app.command("probe-remote")
def io_probe_remote(
    uri: str = typer.Argument(..., help="Remote gzipped FASTQ URL to fingerprint."),
    md5: str | None = typer.Option(
        None, "--md5", help="Provider md5 (ENA fastq_md5) — becomes the content-address."
    ),
    max_reads: int = typer.Option(
        DEFAULT_MAX_READS,
        help="Bounded head read budget (default 2000). Raise it to fingerprint more of the remote "
        "FASTQ — the explicit opt-in; every touch stays bounded by this AND --max-bytes.",
    ),
    max_bytes: int = typer.Option(DEFAULT_MAX_BYTES, help="Bounded decompressed-byte cap."),
    max_compressed_bytes: int = typer.Option(
        8 << 20, help="Compressed bytes to range-read in one GET (the network budget)."
    ),
) -> None:
    """Fingerprint a remote FASTQ into an Observation WITHOUT downloading it (issue #39).

    The remote twin of `probe`: one bounded HTTP Range read is fed through the same Tier-A pipeline, so
    a URL resolves to a library exactly as a local file does — no staging, no local copy. With `--md5`
    the provider's hash is the content-address, matching the hosted bytes with zero body read. Exit 1 if
    the host ignores Range and answers 200 with the whole file (bounded means bounded by the server).
    """
    from ..io.remote import RemoteError, probe_remote

    try:
        obs, _seqs = probe_remote(
            uri,
            md5=md5,
            max_reads=max_reads,
            max_bytes=max_bytes,
            max_compressed_bytes=max_compressed_bytes,
        )
    except (NotYetImplemented, RemoteError, ValueError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(obs.model_dump(mode="json"), indent=2))


@io_app.command("probe-sra")
def io_probe_sra(
    accession: str = typer.Argument(
        ..., help="An SRA run (SRR/ERR/DRR) or experiment (SRX) accession."
    ),
    n_reads: int = typer.Option(
        DEFAULT_MAX_READS,
        help="Bounded head read budget (default 2000; here spots streamed from the .sra). Raise it to "
        "fingerprint more of the run — the explicit opt-in; every touch stays bounded by this.",
    ),
    max_bytes: int = typer.Option(DEFAULT_MAX_BYTES, help="Bounded decompressed-byte cap."),
) -> None:
    """Fingerprint an SRA run into per-mate Observations WITHOUT downloading it.

    The archive twin of `probe-remote`: the first N spots stream straight from the `.sra` into memory
    (no FASTQ on disk) and feed the same Tier-A pipeline, so a run resolves to a library exactly as a
    local file does. ENA metadata is resolved best-effort for the content-address (its `fastq_md5`
    becomes the address when the run was mirrored faithfully); a run ENA never mirrored still fingerprints
    from the stream, with a synthetic SRA-derived address. Exit 1 on a resolution or stream failure.
    """
    from ..io.remote import RemoteError
    from ..io.sra import probe_sra

    try:
        try:
            result = resolve_accession(accession, check_reads=True)
            runs = [
                r
                for r in result["runs"]
                if (r.get("run_accession") or "").upper() == accession.upper()
            ] or result["runs"]
        except RemoteError:
            # Not ENA-mirrored (ERR/DRR, or an unreleased mirror): stream from SRA anyway, no ENA meta.
            runs = [{"run_accession": accession}]
        mates = [
            {
                "run": run.get("run_accession"),
                "read_index": mate.read_index,
                "basename": mate.basename,
                "ena_verified": mate.ena_verified,
                "observation": mate.observation.model_dump(mode="json"),
            }
            for run in runs
            for mate in probe_sra(run, n_reads=n_reads, max_bytes=max_bytes)
        ]
    except (NotYetImplemented, RemoteError, ValueError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps({"accession": accession, "n_mates": len(mates), "mates": mates}, indent=2)
    )


@io_app.command("resolve")
def io_resolve(
    accession: str = typer.Argument(..., help="GSE/GSM, PRJNA/PRJEB, SRP/SRX/SRR, SAMN..."),
    check_reads: bool = typer.Option(
        True,
        "--check-reads/--no-check-reads",
        help="Compare SRA's per-read table to what ENA published (detects a dropped technical read).",
    ),
) -> None:
    """Expand an accession into runs + declared metadata, and flag a dropped technical read.

    The important part is the flag, not the inventory: fasterq-dump skips technical reads BY DEFAULT,
    so a 10x barcode read routinely vanishes from the published FASTQ while staying inside the .sra —
    leaving a dataset that looks like plain single-end RNA-seq. Two metadata calls catch it before a
    byte is downloaded (rung 0). Exit 4 if any run is missing one: a human must re-fetch it.
    """
    from ..io.remote import RemoteError

    try:
        result = resolve_accession(accession, check_reads=check_reads)
    except (NotYetImplemented, RemoteError) as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2))
    if result.get("n_runs_missing_technical_read"):
        raise typer.Exit(4)


@io_app.command("records")
def io_records(
    accession: str = typer.Argument(..., help="GSE/GSM, PRJNA/PRJEB, SRP/SRX/SRR, SAMN..."),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Fetch what the archive DECLARES about a dataset: project, sample, experiment, run.

    A transcriber, not a resolver. It reports the record and stops — `resolve` decides what any of it
    means. This is where per-sample metadata comes from: `strain`, `tissue`, `sex`, `dev_stage` live
    on the BioSample record and were fetched by no code at all until now, which is why the pilot's six
    samples all said `tissue: null`.

    It is also where the files the submitter UPLOADED are listed — name, provider md5, size and the
    `sra-pub-src-*` URI each one can be fetched from. A run that lists none is the normal case: most
    deposits publish no originals.

    Cached under `seqforge/records/`: a record is a fact about the archive at a moment, so
    re-fetching it should be a choice.
    """
    from ..io.archive import fetch_records
    from ..io.remote import RemoteError

    try:
        records = fetch_records(accession)
    except RemoteError as exc:
        typer.echo(json.dumps({"error": str(exc)}, indent=2), err=True)
        raise typer.Exit(1) from exc

    state = records_dir(workspace)
    state.mkdir(parents=True, exist_ok=True)
    target = state / f"{accession}.json"
    target.write_text(json.dumps(records.model_dump(mode="json"), indent=2))
    # The submitted files are listed in full rather than counted, because this verb is where the
    # concrete `s3://sra-pub-src-*` URI reaches a person at all: five refusals now say "go run
    # `seqforge io records`" instead of naming a bucket and an API to go hunting in, and every one of
    # those pointers dead-ends if the answer is a number (`docs/adr/0033`). Flat, one row per file
    # with its run alongside, so a caller filters it with one `jq select`: this output is an
    # interface for a program first and a person second.
    typer.echo(
        json.dumps(
            {
                "records": str(target),
                "query": records.query,
                "source": records.source,
                "n": {
                    level: len(records.at(level))
                    for level in ("project", "sample", "experiment", "run")
                },
                "submitted_files": [
                    {
                        "run": record.accession,
                        "filename": f.filename,
                        "md5": f.md5,
                        "size_bytes": f.size_bytes,
                        "uri": f.uri,
                    }
                    for record in records.at("run")
                    for f in record.submitted_files
                ],
            },
            indent=2,
        )
    )


@io_app.command("attributes")
def io_attributes(
    name: str | None = typer.Argument(None, help="Show one attribute; omit to list them all."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch NCBI's list and rewrite the shipped vocabulary."
    ),
) -> None:
    """NCBI's harmonized BioSample attribute names — the key space a sample fact must use.

    960 curated names with NCBI's own definitions. We enforce against all of them and ask a model for
    a hand-picked few. `condition` is NOT one of them, which is why it is no longer one of ours.
    """
    from ..io.attributes import (
        ATTRIBUTES_URL,
        get_attribute,
        load_attributes,
        parse_ncbi_attributes_xml,
        source_provenance,
        write_attributes,
    )

    if refresh:
        from ..io.remote import _get

        attrs = parse_ncbi_attributes_xml(_get(ATTRIBUTES_URL, timeout=120))
        path = write_attributes(attrs, fetched=_today())
        typer.echo(json.dumps({"wrote": str(path), "n": len(attrs)}, indent=2))
        return

    if name:
        attr = get_attribute(name)
        typer.echo(
            json.dumps(
                {
                    "name": attr.name,
                    "display": attr.display,
                    "description": attr.description,
                    "synonyms": list(attr.synonyms),
                },
                indent=2,
            )
        )
        return
    typer.echo(
        json.dumps(
            {**source_provenance(), "names": sorted(load_attributes())},
            indent=2,
        )
    )


@io_app.command("efo")
def io_efo(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch labels for every CURIE the KB declares."
    ),
) -> None:
    """The EFO labels behind `library.assay` — what `EFO:0009922` is actually called.

    `assay: EFO:0009922` is good standardization and unreadable. The name comes from EFO via EBI's
    OLS4, never from us: a label we maintain by hand drifts from the ontology it claims to quote.
    `--refresh` re-fetches every CURIE the KB's specs declare, so adding a technology is: add the
    spec, run this, commit.
    """
    import json as _json
    import urllib.parse
    import urllib.request

    from ..io.efo import OLS4_TERMS, EfoTerm, iri_for, load_terms, parse_ols4_term, write_terms
    from ..kb import load_all_specs

    if not refresh:
        typer.echo(
            _json.dumps(
                {c: {"name": t.name, "iri": t.iri} for c, t in sorted(load_terms().items())},
                indent=2,
            )
        )
        return

    curies = sorted({c for spec in load_all_specs().values() for c in spec.identity.assay_ontology})
    terms: dict[str, EfoTerm] = {}
    for curie in curies:
        # OLS4 wants the IRI **double**-URL-encoded in the path. A singly-encoded one 404s, which is
        # the kind of thing that belongs in code rather than in someone's memory.
        quoted = urllib.parse.quote(urllib.parse.quote(iri_for(curie), safe=""), safe="")
        with urllib.request.urlopen(OLS4_TERMS + quoted, timeout=60) as response:  # noqa: S310
            terms[curie] = parse_ols4_term(_json.load(response))
    path = write_terms(terms, fetched=_today())
    typer.echo(
        _json.dumps(
            {"wrote": str(path), "terms": {c: t.name for c, t in sorted(terms.items())}}, indent=2
        )
    )


@io_app.command("umi-count")
def io_umi_count(
    cells: list[str] = typer.Argument(
        ..., help="One `sample_id=/path/to.bam` per cell, in the h5ad's row order."
    ),
    assembly: str = typer.Option(
        ..., "--assembly", help="UCSC assembly id the cells were mapped to."
    ),
    annotation: str = typer.Option(
        ..., "--annotation", help="Registered GTF name; its built database is what is read."
    ),
    out: Path = typer.Option(
        ..., "--out", help="Output .h5ad path — one object for the whole plate."
    ),
) -> None:
    """Count every cell of a plate into ONE .h5ad — the fan-in of the SMART-seq3 pipeline.

    One counting job over all N per-cell BAMs, not N jobs and a merge, and it writes the object
    directly: 1440 cells x ~55 000 genes x 4 matrices is ~630 MB of dense text as a TSV, for a
    sparse object several times smaller. `X` is the exonic UMI matrix, three layers carry the rest,
    rows are sample ids and the four read fates are `obs` columns.

    A verb rather than a `run:` block for the same reason `io h5ad` is one: `snakemake -n -p`
    renders a `shell:` while planning and cannot see inside a `run:`, so only a verb is visible to
    compose's wiring gate. The counter takes **no container** — pysam and gffutils are plain
    dependencies, like anndata, and only the aligner needs an environment we do not own.

    The annotation database is resolved here rather than in the workflow module, exactly as `io
    cram` resolves the reference FASTA: liulab-genome registers a GTF as `<name>.gtf` beside the
    `<name>.db` it builds from it, and exposes the first, so the second is derived from it and the
    module below stays strictly typed and testable against a synthetic annotation. Exit 2 on a
    malformed cell argument, exit 3 on an unresolvable annotation or a missing BAM.
    """
    from ..workflows.umite.count import UmiCountError, parse_cells, write_umi_counts

    # The cells are parsed before anything reaches the environment: a typo in an argument is a bad
    # invocation and should not first cost an assembly lookup that may not even be resolvable here.
    try:
        plate = parse_cells(cells)
    except UmiCountError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(2) from exc
    try:
        from genome import (
            Genome,  # untyped lab package; resolved here, off the strict workflow path
        )
    except ImportError as exc:  # pragma: no cover - depends on the host
        typer.echo(json.dumps({"error": f"liulab-genome is not importable: {exc}"}), err=True)
        raise typer.Exit(3) from exc
    try:
        gtf = Path(str(Genome(assembly).get_gtf_path(annotation)))
        written = write_umi_counts(plate, gtf.with_suffix(".db"), out)
    except UmiCountError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    except (KeyError, ValueError) as exc:  # liulab-genome refuses an unregistered annotation name
        typer.echo(
            json.dumps({"error": f"{annotation} is not registered for {assembly}: {exc}"}), err=True
        )
        raise typer.Exit(3) from exc
    typer.echo(json.dumps({"written": str(written)}, indent=2))


@io_app.command("umi-extract")
def io_umi_extract(
    r1: Path = typer.Option(..., "--r1", help="The tagged read's FASTQ (gzipped)."),
    r2: Path = typer.Option(
        ..., "--r2", help="Its mate's FASTQ; paired by POSITION, never by name."
    ),
    geometry: str = typer.Option(
        ...,
        "--geometry",
        help="The read structure compose derived from the element model, e.g. "
        "`R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22`.",
    ),
    sample: str = typer.Option(..., "--sample", help="Cell id; becomes the uBAM's read group."),
    out: Path = typer.Option(..., "--out", help="Output path for the unaligned BAM."),
    read_id: str = typer.Option(
        "R1", "--read-id", help="Which layout read `--r1` is. Refused if the geometry disagrees."
    ),
) -> None:
    """Lift a plate assay's UMI out of R1 and write the pair as a uBAM carrying `UB:Z:`.

    The per-cell half of the counting engine, and a verb rather than a `run:` block for the reason
    `io h5ad` is one: `snakemake -n -p` renders a `shell:` while planning and cannot see inside a
    `run:`, so only a verb is visible to compose's wiring gate. Needs no container — writing a BAM
    through a library is not aligning reads.

    **`--geometry` is a DERIVATION, not a knob, and it arrives as one value.** The anchor and its
    offset, the UMI's offset and length, the trailing motif and the cDNA start are all read off the
    element coordinates by `compose` and rendered into a single config value — the same move
    chromap's `--read-format` makes, and the reason there is no `--anchor`, `--umi-length` or
    `--window` here for anyone to type. Nothing may declare it: the key is in the composer's derived
    set, so a KB backend that states it is refused at load. `--read-id` says which layout read the
    `--r1` file is and is checked against the read the geometry names, so a rule wired to hand over
    the plain mate is refused instead of quietly extracting nothing.

    Exit 3 on a Blocker-shaped refusal: an unreadable geometry, the wrong mate, a half-renamed
    FASTQ, a pair of unequal length, a truncated input.
    """
    from ..workflows.umite.extract import TagGeometry, UmiExtractError, extract_umis

    try:
        structure = TagGeometry.parse(geometry)
        if structure.read_id != read_id:
            raise UmiExtractError(
                f"--read-id {read_id} was handed over as the tagged read, but this layout's UMI is "
                f"on {structure.read_id}. Extracting from the wrong mate finds no tags at all and "
                f"exits 0"
            )
        stats = extract_umis(r1, r2, out, structure, sample=sample)
    except UmiExtractError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(3) from exc
    typer.echo(
        json.dumps({"written": str(out), "read_id": structure.read_id, **stats.to_dict()}, indent=2)
    )
