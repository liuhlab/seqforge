"""`seqforge io`, the half that fronts the ``io`` package: the onlist registry, the network verbs.

The network verbs are peek, the remote probes, accession resolution, archive records and the
benchmark-package publish. The steps a compiled pipeline runs register on the same ``io`` group from
:mod:`seqforge.cli.workflows`, which fronts the ``workflows`` package.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..io import DEFAULT_REGISTRY, HF_BENCHMARK_REPO, Orientation, default_registry
from ..io.remote import NotYetImplemented, peek, resolve_accession
from ..probe import DEFAULT_MAX_BYTES, DEFAULT_MAX_READS
from ..workspace import records_dir
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
                "address_basis": mate.address_basis,
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
    workspace: Path = typer.Option(Path(), "-C", "--workspace", help="Root for seqforge/ state."),
) -> None:
    """Fetch what the archive DECLARES about a dataset: project, sample, experiment, run.

    A transcriber, not a resolver. It reports the record and stops — `resolve` decides what any of it
    means. The run and sample records are what match each file to its sample.

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
    # those pointers dead-ends if the answer is a number (ADR-0033). Flat, one row per file
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
