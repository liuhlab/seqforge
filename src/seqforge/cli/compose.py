"""`seqforge compose` -- compile (dataset, processing) -> Snakefile + config.yaml + units.tsv."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .. import __version__
from ..compose import ComposeError, compose
from ..io import default_registry
from ..kb import load_spec
from ..manifest import (
    ProcessingInputs,
    exit_code_for_report,
    fill_processing,
    validate_manifest,
    validate_processing,
)
from ._common import _load_manifest, _load_processing
from .root import app


@app.command("compose")
def compose_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a validated manifest.yaml."),
    processing_path: Path | None = typer.Option(
        None, "--processing", help="A processing manifest. Omit to use policy defaults."
    ),
    assembly: str | None = typer.Option(
        None, "--assembly", help="Genome, when composing without --processing."
    ),
    annotation: str | None = typer.Option(
        None, "--annotation", help="Registered GTF name, when composing without --processing."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
    outdir: str = typer.Option(
        "results", help="Pipeline output directory (written into the config)."
    ),
    fastq_dir: Path | None = typer.Option(
        None,
        "--fastq-dir",
        help="Where this machine keeps the FASTQs. Without it units.tsv carries bare basenames "
        "and the pipeline cannot find its input.",
    ),
    onlist_dir: Path | None = typer.Option(
        None,
        "--onlist-dir",
        help="Directory of downloaded barcode whitelists (<name>.txt.gz). Checked before the "
        "network, so a compute node with no internet still composes. Env: SEQFORGE_ONLIST_DIR.",
    ),
    sif_dir: Path | None = typer.Option(
        None,
        "--sif-dir",
        envvar="LIU_LAB_PACKAGES",
        help="Directory of prebuilt liulab-runtime images (liulab-runtime_<env>.sif). Used instead "
        "of the ghcr tag when the file is there, for nodes that cannot reach ghcr.io.",
    ),
) -> None:
    """Compile (dataset, processing) -> Snakefile + config.yaml + units.tsv.

    ``--processing`` is optional: a processing manifest exists because someone wanted something
    non-default, and requiring one per dataset would mean 10^4 boilerplate files nobody reads. Either
    way compose writes the fully-resolved, dataset-bound manifest it used to processing.lock.yaml, so
    the run's state is on disk regardless. Exit 3 if a gate fails.
    """
    manifest = _load_manifest(manifest_path)
    report = validate_manifest(manifest)
    if not report.ok:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose an invalid manifest", err=True)
        raise typer.Exit(exit_code_for_report(report))

    if processing_path is not None:
        processing = _load_processing(processing_path)
    else:
        if assembly is None or annotation is None:
            # The one thing with no safe default. Deriving an assembly from experiment.organism would
            # mean choosing hg38 vs hg19 vs T2T on the user's behalf — a policy call, and that map is
            # liulab-genome's job. Refuse, but make the refusal actionable.
            typer.echo(
                f"compose needs a genome: this dataset's organism is taxid "
                f"{manifest.experiment.organism.value}. Pass --assembly/--annotation, or author one "
                f"with `seqforge processing new`.",
                err=True,
            )
            raise typer.Exit(2)
        processing, _ = fill_processing(
            spec=load_spec(manifest.library.chemistry.value[0]),
            dataset=manifest,
            processing=ProcessingInputs(assembly=assembly, annotation_name=annotation),
            seqforge_version=__version__,
        )

    p_report = validate_processing(processing, dataset=manifest)
    if not p_report.ok:
        typer.echo(json.dumps(p_report.model_dump(mode="json"), indent=2), err=True)
        typer.echo("refusing to compose with an invalid processing manifest", err=True)
        raise typer.Exit(exit_code_for_report(p_report))

    try:
        result = compose(
            manifest,
            processing,
            registry=default_registry(offline=False, local_dir=onlist_dir),
            workspace=workspace,
            outdir=outdir,
            fastq_dir=fastq_dir,
            sif_dir=sif_dir,
        )
    except ComposeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    # The one line the human stream owes: a compile that produces fewer samples than the manifest
    # declares must say so where a person reading a terminal will see it. The count is on stdout
    # either way; what this adds is that nobody has to be looking for it.
    if result.admission is not None and result.admission.record_path is not None:
        typer.echo(
            f"{result.admission.summary} below the {result.admission.threshold}-read admission "
            f"floor this chemistry declares. Which, and why: {result.admission.record_path}",
            err=True,
        )
    # The second line the human stream owes. `run_id` folds a hash of the spec that decided this
    # config (ADR-0037), so the key can see whether the params moved and cannot see the thing this
    # line reports: whether the dataset would still be resolved to that spec at all, which every
    # signature in the knowledge base has a vote in. Read off the result rather than compared here:
    # `run` chains the same `compose` call and owes the same disclosure, and a comparison spelled once
    # per verb is the spelling that drifts.
    if result.kb_moved:
        typer.echo(
            f"compiled under knowledge base {result.kb_version}; this manifest's chemistry was "
            f"decided under {result.manifest_kb_version}, and compose loads the spec that decision "
            f"named rather than deciding again. Which spec a dataset resolves to is a "
            f"whole-knowledge-base question — a signature edited anywhere in {result.kb_version} can "
            f"change the answer. Re-run `seqforge manifest fill` to decide the chemistry under it "
            f"too.",
            err=True,
        )
    # A refusal that does not say why is what #267's triage mis-read as a silent pass, so the reason
    # each failing gate carried back goes to the human stream before the exit code goes to the shell.
    for name, verdict in result.gate.items():
        if verdict.status == "fail":
            typer.echo(f"gate.{name}: fail", err=True)
            for line in verdict.reason:
                typer.echo(f"  {line}", err=True)
    if any(v.status == "fail" for v in result.gate.values()):
        raise typer.Exit(3)
