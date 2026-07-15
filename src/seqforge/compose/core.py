"""``compose`` — manifest -> ``config.yaml`` + ``units.tsv`` + a workflow-module selection.

**Emit data, never code (R1).** The composer selects a hand-written, versioned module and emits its
*configuration*; it never writes rule source. It is a pure function of the manifest plus two
versioned inputs recorded in provenance (the KB and the workflow modules) — it needs no FASTQ on
disk, local or remote.

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
from ..models.manifest import Manifest
from ..models.resolve import ComposeResult, ModuleSelection
from ..workflows import get_module
from .params import find_read_with_role, params_gate, render_param


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
    manifest: Manifest,
    *,
    registry: OnlistRegistry | None = None,
    outdir: str = "results",
    threads: int = 8,
) -> ComposePlan:
    """Build the config + units + module selection for a manifest (no side effects)."""
    registry = registry if registry is not None else DEFAULT_REGISTRY
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
    params = _resolve_params(manifest, spec, registry, onlist_files)

    config: dict[str, object] = {
        "chemistry": list(chemistry),
        "genome": {
            "assembly": manifest.processing.genome.value.assembly,
            "annotation": manifest.processing.genome.value.annotation_name,
        },
        "env": manifest.processing.environment.value,
        "threads": threads,
        "outdir": outdir,
        "read_files_in": _read_files_in(manifest, spec),
        "samples": [s.sample_id for s in manifest.experiment.samples],
    }
    config["solo" if spec.backend.module == "map/starsolo" else "bulk"] = params

    return ComposePlan(
        config=config,
        units=_units(manifest),
        module=ModuleSelection(name=module.name, version=module.version, env=module.env),
        spec=spec,
        onlist_files=onlist_files,
    )


def compose(
    manifest: Manifest,
    *,
    registry: OnlistRegistry | None = None,
    workspace: str | Path = ".",
    outdir: str = "results",
    threads: int = 8,
    run_wiring_gate: bool = True,
) -> ComposeResult:
    """Compile a manifest into a runnable pipeline configuration + run the compose gates."""
    p = plan(manifest, registry=registry, outdir=outdir, threads=threads)

    pipeline_dir = Path(workspace) / ".seqforge" / "pipeline"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    config_path = pipeline_dir / "config.yaml"
    units_path = pipeline_dir / "units.tsv"

    for rel, lines in p.onlist_files.items():
        target = pipeline_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n")

    config_path.write_text(yaml.safe_dump(p.config, sort_keys=True))
    units_path.write_text(_units_tsv(p.units))

    from .gates import e2e_gate, wiring_gate

    status, problems = params_gate(manifest, p.spec, p.config)
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
    manifest: Manifest,
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


def _read_files_in(manifest: Manifest, spec: Spec) -> dict[str, str]:
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


def _units(manifest: Manifest) -> list[dict[str, str]]:
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
