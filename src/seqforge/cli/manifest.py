"""`seqforge manifest` -- the DATASET manifest (what the data IS) plus the probe->resolve->partition
pipeline that fills it. `_fill_manifest_pipeline` is the stage body `seqforge run` chains.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
import yaml

from .. import __version__
from ..io import DEFAULT_REGISTRY
from ..io.taxonomy import TaxonomyUnavailable
from ..kb import list_spec_ids, load_spec
from ..manifest import (
    FillError,
    dataset_content_hash,
    dataset_uris,
    exit_code_for_report,
    experiment_from_metadata,
    fill_manifest,
    validate_manifest,
)
from ..models.assertion import Assertion
from ..resolve import Hypothesis, resolve_runs
from ..workspace import legacy_state_dir, state_dir
from ._common import _auto_cpus, _emit, _load_manifest, _resolve_organism, _StageOut
from .root import manifest_app

if TYPE_CHECKING:
    from ..models.observation import Observation
    from ..models.records import ArchiveRecordSet


@manifest_app.command("fill")
def manifest_fill(
    files: list[Path] = typer.Argument(..., help="The dataset's FASTQ .gz files."),
    organism: str | None = typer.Option(
        None,
        "--organism",
        help="NCBI taxid (6239) or scientific name ('Caenorhabditis elegans'). Optional when "
        "--accession is given: the archive record declares the organism. A flag beats the record.",
    ),
    accession: list[str] = typer.Option(
        [],
        "--accession",
        help="Accession(s) for this dataset. Each is FETCHED: the archive's per-sample records are "
        "where strain/tissue/sex/dev_stage come from.",
    ),
    records_path: Path | None = typer.Option(
        None,
        "--records",
        help="An already-fetched record set (`seqforge io records`), instead of fetching now.",
    ),
    assertions: Path | None = typer.Option(
        None,
        "--assertions",
        help="Span-verified assertions from `harvest extract` (seqforge/assertions.json). Without "
        "this, prose contributes nothing and the model might as well not have run.",
    ),
    assert_chemistry: str | None = typer.Option(
        None,
        "--assert-chemistry",
        help="Force a chemistry KB id (e.g. 10x-3p-gex-v3) as the score hypothesis, outranking any "
        "harvested claim. Breaks a genuine byte tie (v2/v3); still only a selector, never evidence.",
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Never reach the network. --accession then REFUSES, never quietly."
    ),
    cpus: int = typer.Option(
        0, "--cpus", help="Parallel probe workers. 0 = auto (min(8, CPUs)); 1 = sequential."
    ),
    workspace: Path = typer.Option(
        Path("."), "-C", "--workspace", help="Root for seqforge/ state."
    ),
) -> None:
    """Probe -> resolve -> assemble the DATASET manifest: what the data IS.

    **Two resolvers, and they answer different questions.** `resolve score` reads the bytes and says
    what the library is. The metadata resolver reads the archive record and any prose and says which
    sample each file is, and what that sample was. Both can refuse; neither is shown the other's
    input.

    **Multi-run by construction.** Files are grouped into runs by name and each run's roles are
    decided from its own bytes, so one sample per run falls out. Hand it all 12 files of a 6-run
    dataset and you get 6 samples, not one guess.

    **An accession is fetched, not decoration.** `--accession PRJNA1027859` pulls the project,
    sample, experiment and run records and joins them to your files. That is where `tissue`, `strain`,
    `sex` and `dev_stage` live; before this they were fetched by no code at all, which is why every
    sample in the pilot's manifest said `tissue: null` under a paper that says "neurons".

    **No accession is fine.** Most sequencing data never had one. You get samples grouped by run with
    no facts attached, exit 0, and a manifest that is quieter and just as true.

    Takes no genome. Choosing a reference is intent, not something you learn by probing bytes, so it
    lives in `seqforge processing new`. Writes manifest.yaml ONLY after a clean validate.
    """
    from ..io.remote import RemoteError

    try:
        records = _load_records(accession, records_path, offline=offline)
    except RemoteError as exc:
        # Decision: no network is a refusal, not a quieter answer. You asked for this accession's
        # facts; a manifest that silently omits them is content-addressed and permanent.
        typer.echo(
            json.dumps({"error": "records_unavailable", "detail": str(exc)}, indent=2), err=True
        )
        raise typer.Exit(3) from exc

    _emit(
        _fill_manifest_pipeline(
            files=files,
            organism=organism,
            records=records,
            assertions=assertions,
            offline=offline,
            workspace=workspace,
            cpus=_auto_cpus(cpus),
            chemistry_override=assert_chemistry,
        )
    )


def _assay_dirname(chemistry: str) -> str:
    """The subdir name for an assay: its chemistry id, made filesystem-safe. Deterministic, no hash."""
    return chemistry.replace("/", "-")


def _fill_one_assay(
    *,
    state: Path,
    result: Any,
    spec: Any,
    observations: list[Any],
    experiment: Any,
    role_of_sha: dict[str, str],
    conflicts: list[Any],
    warnings: list[Any],
    note_workspace: Path | None,
    uris: dict[str, str] | None = None,
) -> _StageOut:
    """Assemble + validate ONE assay's :class:`DatasetManifest` and write it under ``state``.

    ``state`` is the top-level ``seqforge/`` for a single-assay project (byte-identical to before) or
    ``seqforge/<assay>/`` for one of several. Each manifest is a normal single-chemistry manifest, so
    it flows through today's exact fill/validate/hash code.
    """
    try:
        manifest = fill_manifest(
            result=result,
            spec=spec,
            observations=observations,
            registry=DEFAULT_REGISTRY,
            experiment=experiment,
            seqforge_version=__version__,
            role_of_sha=role_of_sha,
            uris=uris,
        )
    except FillError as exc:
        return _StageOut(str(exc), 3, err=True)

    report = validate_manifest(manifest, conflicts=conflicts, warnings=warnings)
    state.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=True)
    # manifest.yaml exists only if it validated clean; otherwise it stays a draft (see _write_manifest).
    target = _write_manifest(state, payload, ok=report.ok)
    out: dict[str, object] = {"manifest": str(target), "report": report.model_dump(mode="json")}
    if note_workspace is not None and (old := legacy_state_dir(note_workspace)) is not None:
        out["note"] = (
            f"{old} is from an older seqforge, which hid its state behind a dot. State now lives in "
            f"{state}/ because it is the output, not plumbing. Nothing reads the old directory; "
            f"delete it when you have what you need."
        )
    return _StageOut(out, exit_code_for_report(report))


def _fill_manifest_pipeline(
    *,
    files: list[Path],
    organism: str | None,
    records: ArchiveRecordSet | None,
    assertions: Path | None,
    offline: bool,
    workspace: Path,
    cpus: int = 1,
    chemistry_override: str | None = None,
    probed: dict[str, tuple[Observation, list[str]]] | None = None,
) -> _StageOut:
    """Probe -> resolve -> metadata -> PARTITION into assays -> assemble + validate each manifest.

    This is the body of ``manifest fill`` with the network I/O lifted to the caller: ``manifest fill``
    and ``seqforge run`` fetch the archive records differently (one refuses on a miss, one caches to
    disk first), so they hand the already-fetched set in. Every exit is a ``_StageOut`` rather than a
    ``typer.Exit`` — the standalone verb prints it and stops, ``run`` folds it into one summary and
    decides whether to continue. Two resolvers, neither shown the other's input; both can refuse.

    A project splits into **assays** — groups of samples that share one chemistry. One assay yields the
    flat top-level layout, byte-identical to before; several yield one ``seqforge/<assay>/manifest.yaml``
    each and an ``{"assays": [...]}`` summary. The "runs must agree" invariant is now per-SAMPLE (a
    sample split across chemistries blocks); across samples, differing chemistries partition.
    """
    from ..resolve import role_of_sha_for
    from ..resolve.records import resolve_metadata

    if chemistry_override is not None:
        # Canonicalize to a real KB id, CASE-INSENSITIVELY — `resolve score` matches ids
        # case-insensitively, so `manifest fill` must accept the same spellings or it would reject a
        # value scoring would have taken. A value that resolves to nothing would otherwise silently
        # no-op (the hypothesis just wouldn't match a candidate) and the operator would only find out
        # after a full compile still escalated; fail fast. (list computed once — Copilot review.)
        spec_ids = list_spec_ids()
        canonical = {s.lower(): s for s in spec_ids}.get(chemistry_override.strip().lower())
        if canonical is None:
            return _StageOut(
                {
                    "error": "unknown_chemistry",
                    "detail": f"--assert-chemistry {chemistry_override!r} is not a known KB chemistry "
                    f"id; one of: {', '.join(sorted(spec_ids))}",
                },
                2,
                err=True,
            )
        chemistry_override = canonical

    organism_taxid: int | None = None
    if organism is not None:
        # A name, or a taxid typed by hand. `harvest` extracts `experiment.organism` as a NAME with a
        # verified span -- the model already does its job -- so the join it needed was a lookup table.
        try:
            organism_taxid = _resolve_organism(organism, offline=offline)
        except TaxonomyUnavailable as exc:
            return _StageOut(str(exc), 2, err=True)

    parsed, subjects = _assertions_and_subjects(assertions)
    # An operator's --assert-chemistry outranks the harvested prose: it is a deliberate, span-checked
    # override for the one thing prose alone cannot settle — a genuine byte tie (10x v2 vs v3). It is
    # still only a SELECTOR / tie-break into `score`, never a ninth evidence test, so it can order the
    # candidates and break a tie the bytes cannot, but it can never overrule what the bytes decide.
    hypothesis = (
        Hypothesis(value=chemistry_override, id="operator", confidence=1.0)
        if chemistry_override is not None
        else _chemistry_hypothesis(parsed)
    )
    multi = resolve_runs(
        [str(f) for f in files],
        # The protocol paragraph (or the operator override), entering `score` as a SELECTOR and a
        # tie-break -- never as evidence.
        hypothesis=hypothesis,
        workspace=workspace,
        use_cache=False,
        cpus=cpus,
        # A fingerprint run hands in pinned stand-in observations for the head-slices, so the resolve
        # verdict (and the manifest hash) reproduces the full FASTQs' without their bytes present.
        _probed=probed,
    )
    # Surface any OPEN conflict / question as a human-editable questions.md (and clear a stale one on a
    # clean re-run) BEFORE the exit-code branch below short-circuits -- `state_dir(workspace)` is exactly
    # what the Stop hook rglobs, so a genuine cross-family disagreement is made visible and enforced.
    _sync_questions(state_dir(workspace), multi.runs)
    if (
        multi.exit_code() != 0
    ):  # a run that itself failed to resolve (no dataset-wide block any more)
        return _StageOut(
            {
                "runs": {r.run_id: r.output.result.model_dump(mode="json") for r in multi.runs},
                "blockers": [b.model_dump(mode="json") for b in multi.blockers],
            },
            multi.exit_code(),
        )

    metadata = resolve_metadata(
        # Identity only: the metadata resolver is handed no probe signal and cannot read one.
        files=[o.file for o in multi.observations],
        records=records,
        assertions=parsed,
        subjects=subjects,
    )
    if metadata.blockers:
        return _StageOut({"blockers": [b.model_dump(mode="json") for b in metadata.blockers]}, 3)

    # The relocated invariant: a single sample whose files span more than one chemistry is a
    # mis-grouping and blocks. Different chemistries across DIFFERENT samples are a legal partition.
    sample_shas = {s.sample_id: list(s.file_shas) for s in metadata.samples}
    if sample_blockers := multi.sample_disagreements(sample_shas):
        return _StageOut({"blockers": [b.model_dump(mode="json") for b in sample_blockers]}, 3)

    groups = multi.by_chemistry()
    if not groups:  # every run carried its own blocker (caught above); nothing to build
        return _StageOut({"error": "no run resolved to a chemistry"}, 3)
    chem_of = multi.chemistry_of_sha()
    multi_assay = len(groups) > 1
    # ONE dataset-wide URI map, computed over EVERY file's common root, shared by every assay. A
    # per-assay fill otherwise re-derives the root from only its own (deeper) subset, so a sample in
    # an `SRX.../` subdir gets a URI missing that segment while `--fastq-dir` is the dataset root --
    # the units path then does not exist and the wiring gate fails. Single-assay is unaffected: its
    # obs already IS `multi.observations`, so this is the identical map and the manifest hash is
    # byte-for-byte unchanged.
    file_uris = dataset_uris(multi.observations)

    def _build(tech: str, runs: list[Any], state: Path, note_ws: Path | None) -> _StageOut:
        if multi_assay:
            obs = [o for o in multi.observations if chem_of.get(o.file.sha256) == tech]
            samples = [
                s for s in metadata.samples if s.file_shas and chem_of.get(s.file_shas[0]) == tech
            ]
            resolution = metadata.model_copy(update={"samples": samples})
        else:
            obs, resolution = multi.observations, metadata
        # Only the BYTE resolver's conflicts block; a metadata disagreement rides in as a warning.
        conflicts = [c for run in runs for c in run.output.result.conflicts]
        try:
            experiment = experiment_from_metadata(
                resolution, obs, organism_taxid=organism_taxid, uris=file_uris
            )
        except FillError as exc:
            return _StageOut(str(exc), 3, err=True)
        return _fill_one_assay(
            state=state,
            result=runs[0].output.result,  # every run of the assay agreed; any one is the assay's
            spec=load_spec(tech),
            observations=obs,
            experiment=experiment,
            role_of_sha=role_of_sha_for(runs),
            conflicts=conflicts,
            warnings=metadata.warnings,
            note_workspace=note_ws,
            uris=file_uris,
        )

    if not multi_assay:
        tech, runs = next(iter(groups.items()))
        return _build(tech, runs, state_dir(workspace), workspace)

    assays: list[dict[str, object]] = []
    worst = 0
    for tech, runs in groups.items():
        n_samples = sum(
            1 for s in metadata.samples if s.file_shas and chem_of.get(s.file_shas[0]) == tech
        )
        out = _build(tech, runs, state_dir(workspace, _assay_dirname(tech)), None)
        worst = max(worst, out.code)
        entry: dict[str, object] = {
            "chemistry": tech,
            "assay_dir": _assay_dirname(tech),
            "n_samples": n_samples,
        }
        entry.update(out.payload if isinstance(out.payload, dict) else {"error": out.payload})
        assays.append(entry)
    return _StageOut({"assays": assays, "n_assays": len(assays)}, worst)


def _write_manifest(state: Path, payload: str, *, ok: bool) -> Path:
    """Write manifest.yaml OR manifest.draft.yaml, and remove the other.

    The removal is the fix. `fill` wrote one name or the other and never unlinked its sibling, so a
    run that failed and was then fixed left `manifest.draft.yaml` sitting next to a good
    `manifest.yaml` forever -- and, far worse, a manifest that USED to validate and now does not left
    the stale clean `manifest.yaml` in place while reporting a draft. Every downstream verb reads
    `manifest.yaml` by name. It would have compiled the old one and said nothing.

    Exactly one of the two exists when this returns. That is the whole contract, and it is what
    "manifest.yaml exists only if it validated clean" was always supposed to mean.
    """
    target = state / ("manifest.yaml" if ok else "manifest.draft.yaml")
    other = state / ("manifest.draft.yaml" if ok else "manifest.yaml")
    target.write_text(payload)
    other.unlink(missing_ok=True)
    return target


def _render_questions(conflicts: list[tuple[str, Any]], questions: list[tuple[str, Any]]) -> str:
    """A human-editable Markdown surfacing of the open conflicts / questions blocking this dataset."""
    lines = [
        "# Open questions for this dataset",
        "",
        "seqforge stopped because the metadata and the FASTQ bytes disagree in a way code will not",
        "settle on its own — this can mean a methods-writing slip or a wrong data-vs-paper pairing, and",
        "either way a human should look. Resolve each item below, then re-run: this file clears itself",
        "once nothing is open.",
        "",
    ]
    for run_id, c in conflicts:
        by = {p.basis: p.value for p in c.positions}
        lines += [
            f"## Conflict `{c.id}` — {c.field}  (run {run_id})",
            f"- **asserted** (from the paper / metadata): `{by.get('asserted', '?')}`",
            f"- **observed** (from the reads): `{by.get('observed', '?')}`",
            f"- decidable by: {', '.join(c.decidable_by)}",
            "",
            "If the paper is right and you have confirmed it, re-run with "
            "`--assert-chemistry=<observed id>` after judging. If the FASTQs do not match this "
            "accession, the data-vs-paper pairing may be wrong — investigate before compiling.",
            "",
        ]
    for run_id, q in questions:
        opts = ", ".join(f"`{o}`" for o in q.options)
        lines += [
            f"## Question `{q.id}` — {q.field}  (run {run_id})",
            f"{q.prompt}",
            f"- options: {opts}",
            f"- decidable by: {', '.join(q.decidable_by)}",
            "",
        ]
    return "\n".join(lines)


def _sync_questions(state: Path, runs: list[Any]) -> None:
    """Write ``state/questions.md`` for open conflicts / questions across runs; clear it when none.

    The Stop hook refuses to end a turn while any non-empty ``questions.md`` exists under ``seqforge/``,
    so a genuine (cross-family) conflict becomes a visible, enforced artifact. Writing AND clearing here
    — before the pipeline's exit-code branch — keeps the two symmetric: a re-run that resolves the
    disagreement removes the file, so the hook cannot wedge on a stale question. A within-family
    difference is recorded as a ``resolved`` conflict, so it is not ``open`` and never writes here.
    """
    open_conflicts = [
        (r.run_id, c) for r in runs for c in r.output.result.conflicts if c.status == "open"
    ]
    open_questions = [(r.run_id, q) for r in runs for q in r.output.result.questions]
    path = state / "questions.md"
    if not open_conflicts and not open_questions:
        path.unlink(missing_ok=True)
        return
    state.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_questions(open_conflicts, open_questions))


def _chemistry_hypothesis(assertions: list[Assertion]) -> Hypothesis | None:
    """The chemistry the prose claims, entering `score` as a hypothesis. ``None`` when it cannot.

    **What this is allowed to do.** `score` builds a grid — one row per read role, one column per
    file — from eight byte-tests, and the hypothesis touches none of them. It orders the candidates
    (so the right whitelist is checked first) and it can break a tie the bytes genuinely cannot
    settle. For prose to move a *score* there would have to be a ninth test, `metadata_says`, and a
    spec could then declare a chemistry that identifies itself by being described rather than by
    what is in its reads. That is the thing we do not build.

    **Agreement or nothing.** Every chemistry claim in the dataset must say the same thing. Two
    experiments describing two protocols is a real dataset, and one dataset-level hypothesis would
    steer both — half of them wrongly. Dropping it costs only a hint: the bytes still decide, and if
    the runs really are two chemistries, `resolve_runs` blocks on the disagreement, which is the right
    answer arrived at honestly.
    """
    values = {a.value for a in assertions if a.field == "library.chemistry"}
    if len(values) != 1:
        return None
    return Hypothesis(value=next(iter(values)), id="harvest", confidence=0.9)


def _load_records(
    accessions: list[str], records_path: Path | None, *, offline: bool
) -> ArchiveRecordSet | None:
    """The archive records for this dataset, or ``None`` if nobody named one.

    ``None`` is the common case and is not a degradation: a plate sequenced last week has no
    accession. But an accession that was *given* and cannot be fetched is a refusal — you asked for
    those facts, and a manifest is content-addressed and never rewritten, so quietly omitting them
    would bake the omission in.
    """
    from ..io.archive import fetch_records
    from ..io.remote import RemoteError
    from ..models.records import ArchiveRecordSet

    if records_path is not None:
        return ArchiveRecordSet.model_validate_json(records_path.read_text())
    if not accessions:
        return None
    if offline:
        raise RemoteError(
            f"--accession {', '.join(accessions)} needs the archive, and --offline forbids it. "
            f"Fetch the records once with `seqforge io records {accessions[0]}` and pass "
            f"`--records`, or drop --accession to compile with no sample facts."
        )
    merged: list[Any] = []
    for acc in accessions:
        merged.extend(fetch_records(acc).records)
    return ArchiveRecordSet(
        source="ncbi-sra+biosample", query=", ".join(accessions), records=merged
    )


def _assertions_and_subjects(path: Path | None) -> tuple[list[Assertion], list[Any]]:
    """Read `harvest extract`'s artifact: the claims, and which record each document came from.

    ``document_subjects`` is the same trick as ``instruction_docs`` beside it — a code-owned mapping
    from document to what code knows about it, written down so a later process can reconstruct it.
    Without it, an assertion's ``doc_sha256`` is an opaque hash and the resolver cannot tell a
    sample's own alias from a paper about six samples, which is the entire difference between a
    declaration and an inference.
    """
    from ..resolve.records import DocumentSubject

    if path is None:
        return [], []
    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        raise ValueError(
            "this looks like a pre-2026.7 assertions.json (a bare list). It cannot say which "
            "document each claim came from, so re-run `seqforge harvest extract`."
        )
    parsed = [Assertion.model_validate(a) for a in payload.get("assertions", ())]
    subjects = [
        DocumentSubject(
            doc_sha256=str(d["doc_sha256"]), scope=str(d["scope"]), subject=d.get("subject")
        )
        for d in payload.get("document_subjects", ())
    ]
    return parsed, subjects


@manifest_app.command("validate")
def manifest_validate(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Validate a manifest. Exit 3 on a Blocker, 4 on an open Conflict."""
    report = validate_manifest(_load_manifest(manifest_path))
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    raise typer.Exit(exit_code_for_report(report))


@manifest_app.command("hash")
def manifest_hash_cmd(
    manifest_path: Path = typer.Argument(..., help="Path to a manifest.yaml."),
) -> None:
    """Print the dataset manifest's content hash and whether it matches the recorded one."""
    manifest = _load_manifest(manifest_path)
    content = dataset_content_hash(manifest)
    typer.echo(
        json.dumps(
            {
                "dataset_hash": content,
                "recorded_hash": manifest.provenance.dataset_hash,
                "matches": content == manifest.provenance.dataset_hash,
            },
            indent=2,
        )
    )
