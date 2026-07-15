"""``compose`` — (dataset, processing) -> ``config.yaml`` + ``units.tsv`` + a module selection.

**Emit data, never code (R1).** The composer selects a hand-written, versioned module and emits its
*configuration*; it never writes rule source. It is a pure function of **both** its inputs plus two
versioned ones recorded in provenance (the KB and the workflow modules) — it needs no FASTQ on disk,
local or remote. Two inputs, still no I/O: a processing manifest is data, not a side channel.

Purity across *both* is what makes "same dataset + different processing = different pipeline, same
dataset hash" a fact rather than a hope. Output is keyed by ``run_id = H(dataset ⊕ processing ⊕ kb ⊕
workflow)``, so composing one dataset two ways yields two directories instead of the second silently
overwriting the first — which is what a fixed ``.seqforge/pipeline/`` path used to do, in exactly the
case the split exists to enable.

The machine-independent/machine-specific boundary lives here: the **manifest** carries URIs and a
``liulab-genome`` assembly id (R9); the **config** it compiles to is the resolved, machine-specific
instantiation, so paths appear here and only here.

The gate has three parts (design §4.1) — this module always runs the deterministic **params** gate;
``wiring`` (snakemake -n/--lint) and ``e2e`` (the real count-matrix run) are reported as ``skip``
when their toolchain is absent, never silently as ``pass``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..io import DEFAULT_REGISTRY, OnlistNotAvailable, OnlistRegistry, PackedOnlist
from ..kb import load_spec
from ..kb.schema import Spec
from ..manifest.hash import run_id
from ..models.dataset import DatasetManifest
from ..models.processing import DatasetPin, ProcessingManifest, Quantification, SoloQuant
from ..models.resolve import ComposeResult, ModuleSelection
from ..workflows import get_module
from .params import (
    derived_params,
    find_read_with_role,
    param_block_key,
    params_gate,
    processing_params,
    render_param,
)


class ComposeError(RuntimeError):
    """The manifest cannot be compiled (unknown chemistry/module, or an unresolvable whitelist)."""


@dataclass(frozen=True)
class ComposePlan:
    """The composed artifacts, before they are written (compose is a pure function of the manifest)."""

    config: dict[str, object]
    units: list[dict[str, str]]
    module: ModuleSelection
    spec: Spec
    onlist_files: dict[str, list[str]]  # relative path -> barcode lines to materialize


def plan(
    manifest: DatasetManifest,
    processing: ProcessingManifest,
    *,
    registry: OnlistRegistry | None = None,
    outdir: str = "results",
) -> ComposePlan:
    """Build the config + units + module selection for one (dataset, processing) pair.

    Both inputs are positional and required. Composing one dataset two ways is literally two calls
    with one argument changed, producing two ``run_id``s over one unchanged ``dataset_hash``.

    ``threads`` is deliberately NOT an argument: it lives in ``processing.resources.threads``. Having
    it in both places was the same two-sources-of-truth disease this whole change is curing.
    ``outdir`` stays an argument, because it is a path — a machine fact, which R9 forbids a manifest
    from carrying. ``threads`` is intent; ``outdir`` is an invocation.
    """
    registry = registry if registry is not None else DEFAULT_REGISTRY
    _check_pin(manifest, processing)
    chemistry = manifest.library.chemistry.value
    if not chemistry:
        raise ComposeError("manifest.library.chemistry is empty; nothing to compile")
    # A processing-equivalent class emits identical params by construction (§12), so the first
    # member compiles for all of them — that is exactly what makes the ambiguity benign.
    spec = load_spec(chemistry[0])
    try:
        module = get_module(spec.backend.module)
    except KeyError as exc:
        raise ComposeError(str(exc)) from exc

    onlist_files: dict[str, list[str]] = {}
    intent = processing.processing
    # Two owners, one block (R14). The KB says how to PARSE; the processing manifest says what to
    # COUNT. params_gate proves the key sets stay disjoint and that both halves arrive verbatim.
    params = _resolve_params(manifest, spec, registry, onlist_files)
    # Derived before processing, both after the KB: three owners, one key each, and `param_owners`
    # is the definition all three agree on. A complex chemistry locates its barcodes by quadruple,
    # computed from the element model rather than hand-entered (see `derived_params`).
    params.update(derived_params(spec))
    params.update(
        {k: render_param(v) for k, v in processing_params(intent.quantification.value).items()}
    )

    config: dict[str, object] = {
        "chemistry": list(chemistry),
        "genome": {
            "assembly": intent.genome.value.assembly,
            "annotation": intent.genome.value.annotation_name,
        },
        "env": intent.environment.value,
        "threads": intent.resources.threads,
        "outdir": outdir,
        "read_files_in": _read_files_in(manifest, spec),
        "samples": [s.sample_id for s in manifest.experiment.samples],
    }
    config[param_block_key(spec)] = params
    primary = _primary_feature(intent.quantification.value)
    if primary is not None:
        # Top level, NOT inside config["solo"] — that block must stay "every key is a STAR CLI flag"
        # for the gate's coverage check, and STARsolo has no --primaryFeature. Order in the manifest
        # is a seqforge-side annotation with no aligner-side referent (STARsolo writes one
        # Solo.out/<Feature>/ per value and does not care about order), so it gets projected out to
        # an explicit value rather than leaving a positional convention load-bearing for every
        # downstream reader.
        config["primary_feature"] = primary

    return ComposePlan(
        config=config,
        units=_units(manifest),
        module=ModuleSelection(name=module.name, version=module.version, env=module.env),
        spec=spec,
        onlist_files=onlist_files,
    )


def _primary_feature(quant: Quantification) -> str | None:
    """Which ``Solo.out/<Feature>/`` is THE matrix for this run. ``None`` for bulk (no such split)."""
    return quant.features[0] if isinstance(quant, SoloQuant) else None


def _check_pin(manifest: DatasetManifest, processing: ProcessingManifest) -> None:
    """A BOUND processing manifest must name this dataset. Never auto-repin (R13)."""
    pin = processing.dataset
    if pin is None:
        return  # a template: portable by design — that is what drives a corpus
    if pin.dataset_hash != manifest.provenance.dataset_hash:
        raise ComposeError(
            f"processing manifest {processing.processing_id!r} is pinned to dataset "
            f"{pin.dataset_hash[:12]}… but this dataset is "
            f"{manifest.provenance.dataset_hash[:12]}…. Re-run `seqforge processing new` against "
            f"this dataset, or drop the pin to make it a template."
        )


def compose(
    manifest: DatasetManifest,
    processing: ProcessingManifest,
    *,
    registry: OnlistRegistry | None = None,
    workspace: str | Path = ".",
    outdir: str = "results",
    run_wiring_gate: bool = True,
) -> ComposeResult:
    """Compile a (dataset, processing) pair into a runnable configuration + run the compose gates."""
    p = plan(manifest, processing, registry=registry, outdir=outdir)

    # Keyed by the RUN, not by the workspace. A fixed `.seqforge/pipeline/` path meant recipe B
    # silently overwrote recipe A's config, units, and materialized onlists — and "one dataset, many
    # recipes" is precisely the case this whole change exists to enable.
    rid = run_id(
        dataset_hash=manifest.provenance.dataset_hash,
        processing_hash=processing.provenance.processing_hash,
        kb_version=manifest.provenance.kb_version,
        workflow_version=processing.provenance.workflow_version,
    )
    pipeline_dir = Path(workspace) / ".seqforge" / "pipeline" / rid
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    config_path = pipeline_dir / "config.yaml"
    units_path = pipeline_dir / "units.tsv"

    for rel, lines in p.onlist_files.items():
        target = pipeline_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n")

    config_path.write_text(yaml.safe_dump(p.config, sort_keys=True))
    units_path.write_text(_units_tsv(p.units))
    # The resolved, dataset-BOUND processing manifest that produced this config, beside it. R7 says
    # disk is state, not that disk is input: the default path takes no --processing and must still
    # leave behind exactly what decided the run.
    bound = (
        processing
        if processing.dataset is not None
        else processing.model_copy(
            update={
                "dataset": DatasetPin(
                    dataset_hash=manifest.provenance.dataset_hash,
                    accessions=list(manifest.experiment.accessions.value),
                )
            }
        )
    )
    (pipeline_dir / "processing.lock.yaml").write_text(
        yaml.safe_dump(bound.model_dump(mode="json"), sort_keys=True)
    )

    from .gates import e2e_gate, wiring_gate

    status, problems = params_gate(manifest, processing, p.spec, p.config)
    gate: dict[str, str] = {
        "params": status,
        "wiring": wiring_gate(pipeline_dir, p) if run_wiring_gate else "skip",
        "e2e": e2e_gate(),
    }

    preview: dict[str, object] = dict(p.config)
    # a failing gate must say WHY; the verdict alone is not actionable (R4's spirit)
    preview["params_problems"] = problems

    return ComposeResult(
        modules=[p.module],
        config_path=str(config_path.relative_to(Path(workspace))),
        units_path=str(units_path.relative_to(Path(workspace))),
        gate=gate,  # type: ignore[arg-type]
        params_preview=preview,
    )


def _resolve_params(
    manifest: DatasetManifest,
    spec: Spec,
    registry: OnlistRegistry,
    onlist_files: dict[str, list[str]],
) -> dict[str, object]:
    """Render the KB backend params for a CLI, resolving every ``{onlist:alias}`` to a real path."""
    out: dict[str, object] = {}
    for key, value in spec.backend.params.items():
        if isinstance(value, list):
            rendered = [_resolve_token(v, spec, registry, onlist_files) for v in value]
            out[key] = " ".join(str(r) for r in rendered)
        else:
            out[key] = render_param(_resolve_token(value, spec, registry, onlist_files))
    return out


def _resolve_token(
    value: object, spec: Spec, registry: OnlistRegistry, onlist_files: dict[str, list[str]]
) -> object:
    if not (isinstance(value, str) and value.startswith("{onlist:") and value.endswith("}")):
        return value
    alias = value[len("{onlist:") : -1]
    ref = spec.onlists.get(alias)
    if ref is None:
        raise ComposeError(f"backend references undeclared onlist alias {alias!r}")
    name = ref.registry
    rel = f"onlists/{name}.txt"
    try:
        packed = registry.packed(name)
    except OnlistNotAvailable as exc:
        raise ComposeError(
            f"onlist {name!r} is not materialized, so --soloCBwhitelist cannot be emitted: {exc}. "
            "Register it (URL + sha256) or run with a registry that can fetch it."
        ) from exc
    onlist_files[rel] = _barcodes(packed)
    return rel


def _barcodes(packed: PackedOnlist) -> list[str]:
    """Unpack a whitelist back to barcode text (the form STARsolo's --soloCBwhitelist expects)."""
    bases = "ACGT"
    width = int(packed.width)
    out: list[str] = []
    for code in packed.codes.tolist():
        chars = []
        c = int(code)
        for _ in range(width):
            chars.append(bases[c & 0b11])
            c >>= 2
        out.append("".join(reversed(chars)))
    return sorted(out)


def _read_files_in(manifest: DatasetManifest, spec: Spec) -> dict[str, str]:
    """Map roles to the reads the module must pass to the aligner, cDNA FIRST for STARsolo."""
    if spec.backend.module == "map/starsolo":
        cdna = find_read_with_role(manifest, "cDNA") or find_read_with_role(manifest, "gDNA")
        barcode = find_read_with_role(manifest, "CB")
        if cdna is None or barcode is None:
            raise ComposeError("a barcoded chemistry needs both a cDNA read and a CB-bearing read")
        return {"cdna": cdna.read_id, "barcode": barcode.read_id}
    reads = manifest.library.read_layout.value.reads
    if len(reads) < 2:
        raise ComposeError(f"bulk paired-end needs 2 reads, found {len(reads)}")
    return {"mate1": reads[0].read_id, "mate2": reads[1].read_id}


def _units(manifest: DatasetManifest) -> list[dict[str, str]]:
    """One row per (sample, read role, file). Falls back to a single implicit sample."""
    by_uri = {f.uri: f for f in manifest.library.files}
    rows: list[dict[str, str]] = []
    samples = manifest.experiment.samples
    if not samples:
        for f in manifest.library.files:
            if f.read_id is not None:
                rows.append({"sample_id": "sample1", "read_id": f.read_id.value, "path": f.uri})
        return rows
    for sample in samples:
        for uri in sample.file_uris:
            item = by_uri.get(uri)
            if item is None or item.read_id is None:
                continue  # unassigned (index/ignored) files never become units
            rows.append(
                {"sample_id": sample.sample_id, "read_id": item.read_id.value, "path": item.uri}
            )
    return rows


def _units_tsv(rows: list[dict[str, str]]) -> str:
    header = ["sample_id", "read_id", "path"]
    lines = ["\t".join(header)]
    lines += ["\t".join(r[h] for h in header) for r in rows]
    return "\n".join(lines) + "\n"
