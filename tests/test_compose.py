"""Tests for ``seqforge.compose`` — plan, the emitted config/units.tsv, and the compose gates.

The composer turns ``(dataset, processing)`` into a Snakefile + config.yaml + units.tsv. The params
gate is the semantic check a dry run cannot make, so it gets adversarial coverage: a KB whose
declared offsets contradict the observed layout, and a config that drops or mangles a
chemistry-defining knob, must both FAIL — silently emitting them is how a corpus gets poisoned.

The shared build helpers (``built_v3``, ``_build``, ``_processing`` …) live in ``tests/conftest.py``.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from conftest import (
    PLATE_CELL_COUNT,
    Built,
    ComposedPlate,
    DryRun,
    SynthDataset,
    _build,
    _processing,
    _rule_blocks,
    _src_root,
    count_matrix,
    declare_read_floor,
    one_run_each,
    plate_of,
    solo_block,
    write_fastq_gz,
)
from seqforge import kb
from seqforge.compose import ComposeError, compose, core, params_gate, plan
from seqforge.compose.params import param_block_key, param_owners
from seqforge.io import OnlistRegistry
from seqforge.manifest.hash import dataset_content_hash
from seqforge.models.dataset import DatasetManifest
from seqforge.models.processing import ProcessingManifest
from seqforge.models.resolve import ComposeResult
from seqforge.pipeline import EXCLUSIONS_NAME
from seqforge.resolve.confuse import canonical_backend
from seqforge.workflows import PLATE_H5AD, get_module, keys_read_by, list_modules
from seqforge.workflows.umite.extract import TagGeometry


def test_compose_10x_emits_kb_params_and_passes_the_params_gate(
    built_v3: Built, tmp_path: Path
) -> None:
    """Everything asserted here is text off disk, so it runs under conftest's stubbed gate.

    It used to take `real_wiring_gate` for exactly one assertion, `gate["wiring"] == "pass"` — the
    claim `test_every_registered_module_wires_into_a_runnable_dag` now owns for all three modules
    rather than for this one dataset. A 1.5s `snakemake` spawn to re-prove it here bought nothing, and
    dropping it puts this test back into `test-fast`.
    """
    manifest, reg = built_v3
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/starsolo"
    assert result.gate["params"] == "pass"
    # e2e stays skip: it is the real count-matrix run and belongs to `seqforge kb e2e`, never to
    # compose. Its toolchain (STAR, liulab-genome, a cluster) is genuinely absent here.
    assert result.gate["e2e"] == "skip"

    # read the path compose REPORTS, not one reconstructed here: the layout is keyed by run_id and a
    # test that hardcodes it is testing its own arithmetic
    pipeline_dir = (tmp_path / result.config_path).parent
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["solo"]["soloCBlen"] == "16"
    assert config["solo"]["soloUMIlen"] == "12"
    assert config["solo"]["soloStrand"] == "Forward"
    # --readFilesIn order: the cDNA read precedes the barcode read
    assert config["read_files_in"] == {"cdna": "R2", "barcode": "R1"}
    # The whitelist token resolved to a PATH, and compose did not write the file. `rule onlist`
    # builds it and `temp()` deletes it: 10x's real v3 list is 111 MB of text, and writing it into
    # every run directory at compile time cost a third of a gigabyte for one dataset compiled three
    # ways -- for a file STAR opens once. Compose still VERIFIES the registry can produce it, which
    # is the compile-time refusal that matters.
    assert config["solo"]["soloCBwhitelist"] == "onlists/3M-february-2018.txt"
    assert not (pipeline_dir / config["solo"]["soloCBwhitelist"]).exists()

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "run", "lane", "read_id", "path"]
    assert len(units) == 3  # header + 2 reads


def test_compose_bd_enhanced_derives_the_adapter_anchored_starsolo_recipe(
    tmp_path: Path, synth_splitseq: SynthDataset, dry_run: DryRun
) -> None:
    """BD Rhapsody Enhanced compiles to the adapter-anchored STARsolo recipe endorsed on STAR #1607.

    The diversity insert floats every offset, so the geometry cannot be a read-start quadruple: compose
    DERIVES `soloAdapterSequence` from the linker elements and anchors the CB/UMI positions to that
    adapter (anchor 2 = its start, anchor 3 = its end). The exact strings are the maintainer-endorsed
    ones — an independent cross-check on the element geometry — and the params gate must still PASS with
    `soloAdapterSequence` now an owned (derived) key.

    This is the file's ONE complex-chemistry RENDERING check, and it spends its single spawn on the
    plan text rather than on `wiring_gate`'s four-character verdict. That closes a real gap: the module
    reads the adapter with `SOLO.get(...)` (`workflows/map/starsolo.smk`), so a regression to
    `return ""` passes both the config assertion and a grep of the shipped source — only the argv
    STAR would actually receive can tell.

    It also stands in for the SPLiT-seq dry run that used to exist alongside it, which is safe only
    while bd's emitted `solo` keys are a SUPERSET of splitseq's — so that is asserted, not assumed.
    Both take the `CB_UMI_Complex` branch, both carry three whitelists, and neither declares
    `soloBarcodeReadLength`; the day a spec gives SPLiT-seq a derived key bd lacks, the config-level
    sweep would stay green and the rendering axis would silently go uncovered.
    """
    manifest, reg = _build(tmp_path, "bd-rhapsody-wta-enhanced-v1")
    processing = _processing(manifest)
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/starsolo"
    assert result.gate["params"] == "pass"

    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    solo = config["solo"]
    assert solo["soloType"] == "CB_UMI_Complex"
    assert solo["soloAdapterSequence"] == "NNNNNNNNNGTGANNNNNNNNNGACA"
    assert solo["soloCBposition"] == "2_0_2_8 2_13_2_21 3_1_3_9"
    assert solo["soloUMIposition"] == "3_10_3_17"
    assert solo["soloStrand"] == "Forward"
    # the diversity insert is absorbed by the adapter, so no read-start start/length is emitted
    assert "soloCBstart" not in solo and "soloUMIstart" not in solo

    # SPLiT-seq's rendering axis rides on this one. `plan` needs no subprocess (0.06s), so the
    # condition that makes the substitution legal is an assertion rather than a code-review note.
    ss = synth_splitseq.manifest
    ss_solo = plan(ss, _processing(ss), registry=synth_splitseq.registry).config["solo"]
    assert isinstance(ss_solo, dict)
    assert set(solo) >= set(ss_solo), (
        "SPLiT-seq emits a solo key bd-enhanced does not, so bd's plan text no longer covers it; "
        f"give SPLiT-seq its own dry run. Missing: {sorted(set(ss_solo) - set(solo))}"
    )

    # ...and what STAR is actually handed. `-p` renders every shell block while planning.
    planned = dry_run(
        (tmp_path / result.config_path).parent, plan(manifest, processing, registry=reg)
    )
    assert f"--soloAdapterSequence {solo['soloAdapterSequence']}" in planned
    assert f"--soloCBposition {solo['soloCBposition']}" in planned
    # The Complex half of the barcode-match mode, in argv, where it is decidable. `1MM` was a literal
    # in `cb_umi_geometry()`'s Complex branch until #198; it is now the KB's, and this asserts BOTH
    # halves of that move — it arrives (a Complex chemistry that named no match type would FATAL on
    # STAR's own default) and it arrives ONCE. A module that kept the old literal alongside the new
    # placeholder would pass the params gate, which only ever inspects the config, while handing STAR
    # two values and honouring the last.
    assert "--soloCBmatchWLtype 1MM " in planned
    assert planned.count("--soloCBmatchWLtype") == 1
    # neither chemistry declares it, and the module reads it with `SOLO.get(...)` so its absence must
    # render as absence, not as a KeyError or an empty flag
    assert "--soloBarcodeReadLength" not in planned


def test_the_composer_records_the_run_each_unit_came_from(built_v3: Built) -> None:
    """units.tsv carries ``run`` and ``lane``, from the two functions in `resolve.group` that own them.

    Recording both is what lets the mapping module order a pooled sample's files without re-parsing
    filenames. The values must be `run_key` and `lane_of`, not a second notion of either -- one
    function owns each, so the column can never disagree with the grouping that formed the sample.
    """
    from seqforge.compose.core import _units
    from seqforge.resolve.group import lane_of, run_key

    manifest, reg = built_v3
    rows = _units(manifest)
    assert rows and all(set(r) >= {"sample_id", "run", "lane", "read_id", "path"} for r in rows)
    for r in rows:
        assert r["run"] == run_key(r["path"])
        assert r["lane"] == lane_of(r["path"])

    # The lane is worth a column only where it is the ONLY thing left distinguishing two files of one
    # mate, which is the four-lane library ADR-0027 fused into one run -- and which the fixture's own
    # `s_R1`/`s_R2` names (no lane token, so `lane == ""` above) cannot show. Rename its files to what
    # bcl2fastq writes and the run must collapse to one while the lanes stay two.
    laned = manifest.model_copy(deep=True)
    files = [
        f.model_copy(update={"uri": f"cell_42_S1_{lane}_{f.read_id}_001.fastq.gz"})
        for lane in ("L001", "L002")
        for f in manifest.library.files
    ]
    laned.library.files = files
    laned.experiment.samples[0].file_uris = [f.uri for f in files]
    rows = _units(laned)
    assert {r["run"] for r in rows} == {"cell_42_S1"}
    assert {(r["read_id"], r["lane"]) for r in rows} == {
        (read_id, lane) for read_id in ("R1", "R2") for lane in ("L001", "L002")
    }

    # ...and the same for the implicit single sample. `_units` has two loops, and the fallback one is
    # what a dataset with no `experiment.samples` compiles through -- a column present on one branch
    # only is a KeyError in the mapping module, on the datasets nobody has a record for.
    laned.experiment.samples = []
    assert {(r["read_id"], r["lane"]) for r in _units(laned)} == {
        (read_id, lane) for read_id in ("R1", "R2") for lane in ("L001", "L002")
    }


def test_a_sample_pooled_across_runs_pairs_and_comma_joins_readfilesin(
    built_v3: Built, tmp_path: Path, dry_run: DryRun
) -> None:
    """A pooled (multi-run) sample must reach STAR comma-joined per mate AND with mates paired by run.

    Two real bugs hid here, both only on multi-run samples -- single-run fixtures never exercised the
    path:
      1. space-joining a mate's files (``{input.cdna} {input.barcode}`` over a multi-file input) made
         STAR read them as extra MATES and segfault;
      2. mates listed in different run order desync STAR ("quality string length is not equal to
         sequence length") -- it pairs cDNA of run K with barcodes of run J.

    Pairing is driven by the units.tsv ``run`` column (seqforge's own grouping), NOT the filename. To
    prove that, the fixture gives run ``r1`` alphabetically-late filenames and run ``r2`` early ones,
    lists the rows scrambled, and asserts the planned command orders both mates by RUN (r1 then r2) --
    the opposite of what sorting by filename would produce. Generalises the fix; nothing is 2-run
    specific.
    """
    manifest, reg = built_v3
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.snakefile_path).parent

    units_path = pipeline_dir / "units.tsv"
    header = units_path.read_text().splitlines()[0].split("\t")
    assert header == ["sample_id", "run", "lane", "read_id", "path"]
    sid = units_path.read_text().splitlines()[1].split("\t")[0]
    # run -> {role -> filename}; filename order is the REVERSE of run order, so a filename sort would
    # mispair. read_files_in is cdna=R2, barcode=R1. The lane is EMPTY here: these are two runs, and a
    # name with no lane token is what `lane_of` reports as `""` -- so this case still asserts that the
    # run column alone decides, with the lane contributing no order.
    f = {
        "r1": {"R1": "z_bc.fastq.gz", "R2": "z_cdna.fastq.gz"},
        "r2": {"R1": "a_bc.fastq.gz", "R2": "a_cdna.fastq.gz"},
    }
    # rows deliberately SCRAMBLED across mates
    rows = [
        [sid, "r2", "", "R2", f["r2"]["R2"]],
        [sid, "r1", "", "R1", f["r1"]["R1"]],
        [sid, "r2", "", "R1", f["r2"]["R1"]],
        [sid, "r1", "", "R2", f["r1"]["R2"]],
    ]
    units_path.write_text("\n".join("\t".join(r) for r in [header, *rows]) + "\n")
    for *_, path in rows:  # `snakemake -n` needs its source inputs to exist
        (pipeline_dir / path).touch()

    # no `plan=`: the whole point is the units.tsv this test just wrote, so it dry-runs the directory
    # as it stands rather than a replica rebuilt from the plan.
    out = dry_run(pipeline_dir)

    # cdna=R2 first, then barcode=R1; each mate comma-joined AND both ordered by run (r1, then r2).
    expected = f"--readFilesIn {f['r1']['R2']},{f['r2']['R2']} {f['r1']['R1']},{f['r2']['R1']}"
    assert expected in out, (
        f"mates must comma-join and pair by the run column; got: "
        f"{[ln for ln in out.splitlines() if 'readFilesIn' in ln]}"
    )


def test_a_sample_pooled_across_lanes_pairs_readfilesin_by_the_lane_column(
    built_v3: Built, tmp_path: Path, dry_run: DryRun
) -> None:
    """A multi-LANE sample is one run (ADR-0027), so the ``lane`` column is what pairs its mates.

    The sibling above cannot express this case. Since the run key went lane-blind, a four-lane library
    is a SINGLE run: ``run`` ties for all eight files and the sort falls through to ``path``. Real
    bcl2fastq names survive that by coincidence -- `..._L001_R1_001` and `..._L001_R2_001` put the
    lane ahead of the mate token, so both mates sort into the same lane order -- and that coincidence
    is precisely why the fixture does NOT use them: it is what would keep a lane-blind sort looking
    green. Here the cDNA names sort in lane order and the barcode names sort against it, so a
    ``(run, path)`` sort hands STAR L001's cDNA with L002's barcodes.

    Worth a spawn because that mispairing is SILENT, unlike the run-order one above: the two
    comma-lists still hold equal read counts, so STAR neither desyncs nor FATALs -- it exits 0 having
    written a matrix that pairs one lane's barcodes with another lane's cDNA.
    """
    manifest, reg = built_v3
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.snakefile_path).parent

    units_path = pipeline_dir / "units.tsv"
    header = units_path.read_text().splitlines()[0].split("\t")
    sid = units_path.read_text().splitlines()[1].split("\t")[0]
    # lane -> {role -> filename}, ONE run. cdna (R2) sorts lexically in lane order, barcode (R1)
    # sorts against it, so only a lane-aware sort agrees with itself across the two mates.
    f = {
        "L001": {"R1": "z_bc.fastq.gz", "R2": "b_cdna.fastq.gz"},
        "L002": {"R1": "a_bc.fastq.gz", "R2": "c_cdna.fastq.gz"},
    }
    # rows deliberately SCRAMBLED across mates; written through the header, so a column added later
    # cannot silently shift these values into the wrong field
    rows = [
        {"sample_id": sid, "run": "r1", "lane": lane, "read_id": role, "path": f[lane][role]}
        for lane, role in (("L002", "R2"), ("L001", "R1"), ("L002", "R1"), ("L001", "R2"))
    ]
    lines = ["\t".join(header)] + ["\t".join(r[h] for h in header) for r in rows]
    units_path.write_text("\n".join(lines) + "\n")
    for r in rows:  # `snakemake -n` needs its source inputs to exist
        (pipeline_dir / r["path"]).touch()

    out = dry_run(pipeline_dir)

    expected = (
        f"--readFilesIn {f['L001']['R2']},{f['L002']['R2']} {f['L001']['R1']},{f['L002']['R1']}"
    )
    assert expected in out, (
        f"one run's mates must be ordered by the lane column; got: "
        f"{[ln for ln in out.splitlines() if 'readFilesIn' in ln]}"
    )


def test_compose_emits_a_snakefile_even_when_no_gate_runs(
    built_v3: Built, tmp_path: Path, gate_that_must_not_run: None
) -> None:
    """The Snakefile is the DELIVERABLE, so nothing optional may be its reason for existing.

    It used to be written inside `wiring_gate`, after an early `return "skip"` when `snakemake` was
    absent from PATH — and `snakemake` was in no dependency table, so that branch always taken. The
    product of the compiler was a side effect of a validation step that could not fire, and `compose`
    exited 0 having emitted nothing runnable.

    `run_wiring_gate=False` is the sharpest way to state the invariant: no gate ran, and the
    deliverable is still on disk and still complete.

    It un-stubs the gate *because* it is the test that no gate ran: under conftest's stub the gate
    returns the literal `"skip"` for everyone, so `gate["wiring"] == "skip"` would hold even if
    `run_wiring_gate` were ignored entirely. The assertion only means something when the thing it
    asserts was NOT taken is the thing that would otherwise have run.

    It takes `gate_that_must_not_run` rather than `real_wiring_gate` because those are two different
    requests. `real_wiring_gate` means "I spawn `snakemake`", which is what the `external` marker is
    derived from; this test spawns nothing and costs 0.01s, so it belongs in `test-fast`. The fixture
    counts calls and fails at teardown if the gate is reached, which is what keeps that a mechanism.
    """
    manifest, reg = built_v3
    result = compose(
        manifest, _processing(manifest), registry=reg, workspace=tmp_path, run_wiring_gate=False
    )
    assert result.gate["wiring"] == "skip"
    snakefile = tmp_path / result.snakefile_path
    assert snakefile.is_file(), "compose ran a gate-free path and emitted no Snakefile"
    assert get_module("map/starsolo").snakefile.name in snakefile.read_text()


def test_the_wiring_gate_fails_a_workflow_that_plans_nothing(
    built_v3: Built, tmp_path: Path, real_wiring_gate: None
) -> None:
    """A dry run that plans zero jobs exits 0. The gate must not read that as success.

    This is what the wrapper did for the life of the repo: `configfile:` + `include:` parses clean,
    lists every rule, and plans **nothing**, because an `include:`d rule is not a default target.
    Exit code 0. A gate that only checks the return code cannot tell "correct" from "did nothing",
    so it has to look at the output.

    Rather than trust that argument, this builds the broken wrapper on purpose and asserts the gate
    catches it.
    """
    from seqforge.compose.gates import wiring_gate

    manifest, reg = built_v3
    p = plan(manifest, _processing(manifest), registry=reg)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.yaml").write_text(yaml.safe_dump(p.config, sort_keys=True))
    (run_dir / "units.tsv").write_text(core._units_tsv(p.units))
    for rel, lines in p.onlist_files.items():
        t = run_dir / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("\n".join(lines) + "\n")
    module = get_module(p.module.name)
    # the OLD wrapper, verbatim in shape: include: instead of module/use rule
    (run_dir / "Snakefile").write_text(
        f'configfile: "config.yaml"\ninclude: "{module.snakefile.resolve()}"\n'
    )
    assert wiring_gate(run_dir, p) == "fail", (
        "an include:-only wrapper plans zero jobs and exits 0; the gate must not call that a pass"
    )


def test_the_composed_pipeline_plans_the_h5ad_the_whitelist_and_the_command_star_receives(
    built_v3: Built, tmp_path: Path, dry_run: DryRun
) -> None:
    """Everything the DEFAULT 10x plan text has to say, off one `snakemake -n -p`.

    Three tests used to read the plan of this same compose — the h5ad deliverable, the temporary
    whitelist, and `--soloBarcodeReadLength` — and the whitelist one paid twice, once for its own dry
    run and once for a `real_wiring_gate` whose `gate["wiring"] == "pass"` is owned by
    `test_every_registered_module_wires_into_a_runnable_dag[map/starsolo]`. One plan, four claims.

    1. **The deliverable.** `rule all` used to demand `directory(Solo.out)`, so a green pipeline ended
       in a folder of Matrix Market files — and STAR writing three of five features and exiting 0 was
       indistinguishable from success, since the directory existed either way.
    2. **The whitelist has a producing job and is temporary.** A rule declared above `rule all`
       becomes the workflow's default target, and a default target with a wildcard is a hard snakemake
       error — which is exactly what the first attempt at `rule onlist` did. A dry run is the only
       thing that knows.
    3. **`--soloBarcodeReadLength 0` reaches STAR.** The module reads it with `SOLO.get(...)`, not a
       subscript, so that it stays optional for chemistries that do not declare it; nothing but the
       argv proves the 10x half of that still arrives.
    4. **The whole STARsolo command line, as STAR would receive it** (#198, #205): the KB-owned
       barcode match mode, the five hardcoded CellRanger-equivalence flags, the multimapper flag we
       rejected, the sorted-BAM-plus-CB/UB pair that is the only reason the retained CRAM has a
       barcode at all, and `--outSAMmultNmax 1`. Each is a *source* claim everywhere else — a literal
       in a shell block, a key in a spec — and a source claim about a command is not a claim about a
       command. The argv is the ONLY place a module literal is visible at all.
    5. **The exact `--limitBAMsortRAM` byte count**, computed from the `mem_mb` this compose emitted.
       Not cosmetic: it is the only end-to-end proof that the `params:` callable really is handed
       `resources`, and that snakemake resolves `mem_mb` for the attempt *before* evaluating it. Wire
       that wrong and the dry run raises rather than plans; have the callable quietly fall back to
       some other number — STAR's default, a literal, a mis-scaled MiB — and the `\\d+` this replaced
       matched it just as happily. What no argv can show is `resources.mem_mb` vs `config["mem_mb"]`,
       since on attempt 1 they are the same number; that is why
       `test_the_star_rule_escalates_its_memory_on_retry` reads the rule's source shape instead.

    All five read the actual plan rather than an exit code, for the reason the gate does: a dry run
    that plans NOTHING also exits 0.
    """
    manifest, reg = built_v3
    processing = _processing(manifest)
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.snakefile_path).parent

    planned = dry_run(pipeline_dir, plan(manifest, processing, registry=reg))

    assert "solo_to_h5ad" in planned, "the packaging step is not reachable from the default target"
    # `-p` renders every shell block while planning, which is the only reason any of this is visible
    # (a `run:` block would be opaque here) — and it is why the packaging step is a `shell:`.
    assert "seqforge io h5ad" in planned
    sample = manifest.experiment.samples[0].sample_id
    assert f"rule all:\n    input: results/{sample}/{sample}.h5ad" in planned, (
        f"the default target is not the deliverable. Planned:\n{planned}"
    )
    assert f"{sample}.velocyto.h5ad" in planned

    assert "rule onlist" in planned, "the whitelist has no producing job in the plan"
    assert "3M-february-2018" in planned

    assert "--soloBarcodeReadLength 0" in planned

    # The Simple half of the barcode-match mode. Its twin lives in the bd-enhanced dry run, and the
    # pair is the point: `1MM_multi_Nbase_pseudocounts` is CellRanger >=3's correction and is
    # Simple-ONLY, `1MM` is what the four Complex specs carry, and the legality matrix has no value
    # that is both correct and universal. This is the class of change that leaves all seven 10x specs
    # green and breaks only the Complex ones, so the two halves are asserted in two chemistries.
    assert "--soloCBmatchWLtype 1MM_multi_Nbase_pseudocounts" in planned

    # The CellRanger >=4 equivalence set, hardcoded in the module because none of it varies by
    # chemistry — but "hardcoded" is a claim about the source, and only argv proves the flags survive
    # into the command. Without them we emit STARsolo-default counts, which are not comparable to
    # published CellRanger matrices; the whole point of the corpus is that they are.
    for flag in (
        "--clipAdapterType CellRanger4",
        "--outFilterScoreMin 30",
        "--soloUMIfiltering MultiGeneUMI_CR",
        "--soloUMIdedup 1MM_CR",
        "--soloCellFilter EmptyDrops_CR",
    ):
        assert flag in planned, f"{flag} does not reach STAR"
    # ...and the one scRecounter flag we REJECTED. 87% of the multi-gene signal on the measured
    # library was the tandem rDNA array, and all four multimapper matrices are fractional, which
    # breaks pseudobulk. An absence is only a decision if something notices it being reversed.
    assert "--soloMultiMappers" not in planned

    # The barcode itself: STARsolo writes only the cDNA mate, so with no CB/UB tag the barcode is
    # irrecoverably absent from the retained CRAM — which is what made 920 GiB of it unable to
    # recount. STAR emits those tags in the sorted BAM and nowhere else, so the two flags are one
    # decision and are asserted together, along with the sort budget the sort then needs.
    assert "--outSAMtype BAM SortedByCoordinate" in planned
    assert "--outSAMattributes NH HI AS nM CB UB" in planned

    # One alignment per read reaches the BAM (#205). STAR emits every alignment of a multi-mapping
    # read and coordinate-sorts them all, and `seqforge io cram` then drops the secondaries with
    # `-F 0x100`: 198.8M records sorted against 162.9M retained on the measured sample, ~18% of the
    # sort spent producing bytes the very next rule deletes. `nTrOutWrite = min(P.outSAMmultNmax,
    # nTrOutSAM)` writes exactly one top-scoring alignment, which is the record that filter keeps, so
    # both the sort budget and the wall-clock are cheaper. The counts are unaffected; the CRAM is NOT
    # byte-identical for a multimapper (`HI`, and which of several tied loci is retained) — ADR-0023
    # carries the source lines. Like the CellRanger set above it is a module literal, and argv is the
    # only place a module literal is visible at all.
    assert "--outSAMmultNmax 1" in planned

    # The sort budget, to the byte. Both numbers are LITERALS rather than a call to `bam_sort_ram`:
    # recomputing an expectation with the shipped formula cannot fail (`docs/agents/testing.md`,
    # "Adding a test"), and it would agree with a wrong formula as readily as a right one. 48 GiB is
    # `ResourceHints.mem_gb`'s default, so 49152 MiB is what the composer emits and 36 GiB — 3/4 of
    # it, in BYTES — is what STAR must be handed. The exactness is what proves the wiring: the number
    # is produced by a real `snakemake -n` resolving a `resources:` callable, and the `\d+` this
    # replaced matched a fall-back constant, a mis-scaled MiB figure and STAR's own default equally
    # well. What argv cannot show is that the cap FOLLOWS the attempt, since attempt 1 is the only one
    # a dry run renders — `test_a_snakemake_retry_re_expands_a_resource_and_never_a_param` and
    # `test_the_star_rule_escalates_its_memory_on_retry` (`tests/test_workflows.py`) own that half.
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["mem_mb"] == 48 * 1024, "the default memory request moved; restate the cap below"
    assert "--limitBAMsortRAM 38654705664" in planned, (
        "STAR's default of 0 means 'reuse the genome allocation', which is too small on a small "
        "genome and FATALs; the module must pass 3/4 of the memory THIS attempt was granted. "
        f"Planned: {[ln for ln in planned.splitlines() if 'limitBAMsortRAM' in ln]}"
    )


def test_no_run_directive_rule_declares_a_container() -> None:
    """A `container:` on a `run:` rule is ACCEPTED AND SILENTLY IGNORED — so declaring one is a lie.

    Measured against snakemake's own source on 2026-07-15, not recalled from the docs: the container
    wrap lives in `snakemake/shell.py` and therefore only ever wraps a `shell:` command. A `run:`
    block executes Python in the snakemake process and never passes through it. Snakemake's own
    linter agrees — it excludes `is_run` rules from its "missing software definition" check.

    That makes this exactly the failure class this repo is built against: the directive parses, the
    dry run is clean, the pipeline exits 0, and the software was never pinned. Nothing else in the
    stack would say so, so this test does. `genome_index` is the rule that would tempt someone.
    """
    for name in list_modules():
        for rule, body in _rule_blocks(get_module(name).snakefile).items():
            if re.search(r"^\s{4}run:$", body, re.M):
                assert not re.search(r"^\s{4}container:", body, re.M), (
                    f"{name}:{rule} declares a container on a `run:` rule, where snakemake ignores "
                    f"it. Make it a `shell:` (see `solo_to_h5ad`) or drop the directive."
                )


def test_the_aligner_rule_runs_in_a_pinned_container() -> None:
    """The env name was recorded and read by nothing, so every run used whatever aligner was on PATH.

    Generalized over aligners AND over every runtime binary a rule reaches: every module must run its
    external aligner (and any other runtime tool, e.g. samtools/htslib behind `seqforge io cram`)
    inside the pinned `config["container"]`, never from the submitting shell's PATH. Rather than name
    STAR — which only the RNA modules invoke — assert each module carries at least one
    `container: config["container"]` rule and emits the `container` key those rules read. A second
    aligner (chromap, in `align-dna`) is then covered by construction rather than needing its own case.

    The `seqforge io cram` half is folded in from test_the_cram_rule_runs_in_a_pinned_container: it is
    the SAME guard, generalised — the line for a `container:` is "the rule ends up invoking an external
    runtime binary", not "is the aligner". The pure-seqforge steps (h5ad, onlist, the qc bundle) reach
    no such binary and correctly carry no container.
    """
    saw_cram = False
    for name in list_modules():
        blocks = _rule_blocks(get_module(name).snakefile)
        containered = [r for r, b in blocks.items() if 'container: config["container"]' in b]
        assert containered, (
            f"{name} pins no rule in a container, so its aligner runs from whatever the submitting "
            f"shell happened to have"
        )
        # ...and the composer must actually emit the key those rules read.
        assert "container" in get_module(name).required_config
        # a rule shelling out to samtools via `seqforge io cram` is the same guard: the tool it reaches
        # must be the pinned one, not whatever the submitting shell happened to have.
        for rule, body in blocks.items():
            if "seqforge io cram" in body:
                saw_cram = True
                assert 'container: config["container"]' in body, (
                    f"{name}:{rule} runs `seqforge io cram` (which shells out to samtools) with no "
                    f"container, so the tool is whatever the submitting shell happened to have"
                )
    assert saw_cram, "no rule runs `seqforge io cram`; this test is looking at the wrong place"


def test_star_rules_clear_startmp_before_running_so_reruns_are_preemption_safe() -> None:
    """A preempted STAR leaves `_STARtmp` behind and ABORTS a rerun if it already exists.

    On a preemptible partition every requeued alignment failed: STAR refuses to reuse `_STARtmp`, and
    snakemake cannot clean it because it is an undeclared output. So every STAR-invoking rule removes
    its own `_STARtmp` before invoking STAR, and it must do so *before* the STAR command or the abort
    still fires. Both mapping modules invoke STAR (`starsolo_count`, `star_count`) and pass
    `{params.prefix}` (= `results/<sample>/`) as `--outFileNamePrefix`, so each clears
    `results/<sample>/_STARtmp`. Swept over every module, so a third one cannot forget.
    """
    seen = False
    for name in list_modules():
        for rule, body in _rule_blocks(get_module(name).snakefile).items():
            star = body.find("STAR --runMode alignReads")
            if star == -1:
                continue
            seen = True
            cleanup = body.find("rm -rf {params.prefix}_STARtmp")
            assert cleanup != -1, (
                f"{name}:{rule} invokes STAR but never clears `_STARtmp`, so a preempted rerun aborts"
            )
            assert cleanup < star, (
                f"{name}:{rule} clears `_STARtmp` AFTER invoking STAR, which is too late — STAR "
                f"aborts on the stale dir before the cleanup runs"
            )
    assert seen, "no module invokes STAR; this test is looking at the wrong place"


def test_a_prebuilt_sif_beats_the_ghcr_tag_but_only_if_it_is_really_there(tmp_path: Path) -> None:
    """A compute node that cannot reach ghcr.io cannot pull; the lab prebuilds these images.

    The naming (`liulab-runtime_<env>.sif`) is read off liulab-runtime's own `build-sifs.sh`, whose
    header says apptainer is not even installed on the arc login node. A *missing* file falls back to
    the tag rather than emitting a path to nothing: a config naming an absent SIF fails on a node.

    We NAME liulab-runtime's artifact; naming is the opposite of defining. The dropped
    test_the_container_is_a_liulab_runtime_env_and_nothing_is_defined_here asserted
    `container_uri(env) == f"docker://{RUNTIME_IMAGE}:{env}"` over four envs — a straight restatement
    of the function's own return expression with no per-env branch, so it could not fail; the
    fall-back-to-tag case it stood for is exercised here off a real filesystem.
    """
    from seqforge.workflows import container_uri

    assert container_uri("align-rna", tmp_path).startswith("docker://")  # empty dir -> tag
    sif = tmp_path / "liulab-runtime_align-rna.sif"
    sif.touch()
    assert container_uri("align-rna", tmp_path) == str(sif.resolve())


def test_compose_refuses_a_recipe_whose_env_cannot_supply_the_aligner(
    built_v3: Built, tmp_path: Path
) -> None:
    """`map/starsolo` in the `ml` env is a container with no STAR in it — refuse, never correct."""
    manifest, reg = built_v3
    processing = _processing(manifest)
    section = processing.processing.model_copy(
        update={"environment": processing.processing.environment.model_copy(update={"value": "ml"})}
    )
    broken = processing.model_copy(update={"processing": section})

    with pytest.raises(ComposeError, match="align-rna"):
        compose(manifest, broken, registry=reg, workspace=tmp_path)


def test_policy_takes_the_runtime_env_from_the_module_that_needs_it() -> None:
    """One owner. It was hardcoded `"align-rna"` beside a module that also declared `align-rna`."""
    for tech in kb.runnable_spec_ids():
        spec = kb.load_spec(tech)
        from seqforge.manifest.policy import processing_defaults

        assert (
            processing_defaults(spec).environment == get_module(spec.require_backend().module).env
        )


def test_compose_bulk_selects_plain_star(synth_bulk_pe: SynthDataset, tmp_path: Path) -> None:
    """The two-mate case, unchanged by the `mates` widening: BOTH keys, in layout order.

    Half of a pair. Its sibling below composes the same dataset with one mate, and both assert at the
    composed-config seam rather than on `_read_files_in`, because the config is what the module reads
    and a composer internal is not what a wrong answer would reach STAR through.
    """
    manifest, reg = synth_bulk_pe.manifest, synth_bulk_pe.registry
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["bulk"]["quantMode"] == "GeneCounts"
    assert config["read_files_in"] == {"mate1": "R1", "mate2": "R2"}
    assert "solo" not in config


def _one_mate(manifest: DatasetManifest) -> DatasetManifest:
    """The same bulk dataset with its second mate removed — a hand-written **one-read Manifest**.

    Hand-written on purpose, and it stayed hand-written after the KB gained a single-end read set: the
    composer and `map/star` read the MANIFEST's read layout, never the spec's declared reads, so
    deriving this fixture from the resolver would make three composer claims depend on the byte
    resolver agreeing. `test_a_single_end_bulk_deposit_compiles_end_to_end` is the other half — the
    genuinely-resolved one — and having both is what separates "the composer tolerates a one-read
    layout" from "a single-end deposit compiles". So the fixture is the shipped two-read manifest with
    R2 taken out of all three places that carry it — the layout, the file inventory, and the sample's
    file list — which is exactly the shape a resolver that decided single-end fills.

    The dataset hash is recomputed rather than inherited. A manifest whose hash disagrees with its own
    content is a *different* defect, and one that would be sitting inside every assertion below
    pretending to be this test's subject.
    """
    single = manifest.model_copy(deep=True)
    keep = single.library.read_layout.reads[0].read_id
    single.library.read_layout.reads = [
        r for r in single.library.read_layout.reads if r.read_id == keep
    ]
    single.library.files = [f for f in single.library.files if f.read_id == keep]
    for sample in single.experiment.samples:
        sample.file_uris = [f.uri for f in single.library.files if f.uri in sample.file_uris]
    return single.model_copy(
        update={
            "provenance": single.provenance.model_copy(
                update={"dataset_hash": dataset_content_hash(single)}
            )
        }
    )


def test_compose_a_one_mate_layout_emits_the_first_mate_key_alone(
    synth_bulk_pe: SynthDataset, tmp_path: Path
) -> None:
    """A `mates` layout is 1..2 reads, so one read COMPOSES rather than being refused.

    `_read_files_in` used to raise `bulk paired-end needs 2 reads`, which made a single-end library
    uncompilable however it got resolved — the prefactor this ticket exists to remove, so that the
    byte resolver has something able to run a single-end library by the time it starts deciding one.

    Three claims, one compose, all off disk: the config carries `mate1` and no `mate2`, the params
    gate PASSES (a gate that refused the very layout the composer just emitted would be the composer
    and its checker disagreeing about the same widening), and `units.tsv` carries the one mate — a
    units table still listing R2 would hand STAR a file the config never named.
    """
    manifest = _one_mate(synth_bulk_pe.manifest)
    result = compose(
        manifest, _processing(manifest), registry=synth_bulk_pe.registry, workspace=tmp_path
    )
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"

    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["read_files_in"] == {"mate1": "R1"}

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "run", "lane", "read_id", "path"]
    assert len(units) == 2, f"one mate, one sample -> header + one row; got {units}"
    assert units[1].split("\t")[3] == "R1"


def test_a_single_end_bulk_deposit_compiles_end_to_end(
    synth_bulk_se: SynthDataset, tmp_path: Path
) -> None:
    """The whole point of read sets, from one FASTQ to a Snakefile: resolve -> fill -> compose.

    Every claim here is one the BYTE RESOLVER made, not one a fixture trimmed: the deposit is a single
    file, so the manifest's one-read layout, its one-file inventory and the `mate1`-only config all
    follow from the resolver having selected the `se` read set. Its sibling above states the composer
    half against a hand-written layout; this one states that the two halves meet, which is the claim
    that was impossible while a single-end deposit exited 3 with `UNSUPPORTED_TECHNOLOGY`.
    """
    manifest, reg = synth_bulk_se.manifest, synth_bulk_se.registry
    assert manifest.library.chemistry.value == ["bulk-rnaseq"]
    assert [r.read_id for r in manifest.library.read_layout.reads] == ["R1"]
    assert [f.read_id for f in manifest.library.files] == ["R1"]

    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["read_files_in"] == {"mate1": "R1"}
    assert config["bulk"]["quantMode"] == "GeneCounts"
    assert "solo" not in config
    assert (tmp_path / result.snakefile_path).is_file()  # the deliverable a user submits

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert len(units) == 2, f"one read, one sample -> header + one row; got {units}"
    assert units[1].split("\t")[3] == "R1"


def test_the_params_gate_refuses_a_mate_the_layout_does_not_have(
    synth_bulk_pe: SynthDataset,
) -> None:
    """Prove the mates check still fires — in the direction the widening opened.

    The bulk branch used to ask only whether each emitted mate named SOME layout read and whether the
    two differed. That was sufficient while every mates layout had exactly two reads and is not any
    more: a one-read layout carrying a `mate2` that points back at its only read answers both of those
    questions and hands STAR the same FASTQ twice, which doubles every count and exits 0. So the
    branch compares the whole derivation, in order, the way the two barcoded branches always have —
    and a guard nobody has watched fail is a guard that may not be looking.
    """
    manifest = _one_mate(synth_bulk_pe.manifest)
    processing = _processing(manifest)
    spec = kb.load_spec(manifest.library.chemistry.value[0])
    config = plan(manifest, processing, registry=synth_bulk_pe.registry).config
    assert params_gate(manifest, processing, spec, config) == ("pass", []), (
        "the clean pair must pass"
    )

    poisoned = {**config, "read_files_in": {"mate1": "R1", "mate2": "R1"}}
    status, problems = params_gate(manifest, processing, spec, poisoned)
    assert status == "fail"
    assert any("mate2" in p for p in problems), problems


def test_star_is_handed_one_mate_for_a_one_read_layout_and_two_for_a_two_read_one(
    synth_bulk_pe: SynthDataset, tmp_path: Path, dry_run: DryRun
) -> None:
    """What STAR is actually handed, both ways, off one fixture — the contrast IS the claim.

    A config key the module never reaches is a config key that decides nothing, and `star.smk`
    dereferenced `config["read_files_in"]["mate2"]` in two places: the rule's second input, and the
    `--readFilesIn` rendering. Both die at Snakemake parse time on a one-mate config, which is a
    compute node's failure and not a compile-time one — so the one-mate half is asserted through the
    plan text rather than through the emitted config, which cannot see a `KeyError`.

    The two-mate half is here rather than left implicit because a one-sided assertion is satisfied by
    a module that dropped `mate2` for EVERYBODY: single-end is a correct rendering of a paired-end
    library's first mate, it exits 0, and it silently maps half the data.
    """
    reg = synth_bulk_pe.registry
    r1, r2 = (p.name for p in synth_bulk_pe.paths)
    planned: dict[str, str] = {}
    for label, manifest in (
        ("one", _one_mate(synth_bulk_pe.manifest)),
        ("two", synth_bulk_pe.manifest),
    ):
        processing = _processing(manifest)
        result = compose(manifest, processing, registry=reg, workspace=tmp_path)
        pipeline_dir = (tmp_path / result.snakefile_path).parent
        planned[label] = dry_run(pipeline_dir, plan(manifest, processing, registry=reg))

    def readfilesin(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if "readFilesIn" in ln]

    assert f"--readFilesIn {r1} --readFilesCommand" in planned["one"], readfilesin(planned["one"])
    assert f"--readFilesIn {r1} {r2} --readFilesCommand" in planned["two"], readfilesin(
        planned["two"]
    )
    # ...and the second mate is nowhere in the one-mate plan at all: `input.mate2` resolves to EMPTY,
    # rather than to a stale file the units table no longer lists.
    assert r2 not in planned["one"], f"a one-mate layout planned the second mate's file: {r2}"


# ---- the plate pipeline: a fourth layout kind, and a geometry nobody declares --------------------
#
# `map/star-umi` has no chemistry yet (module first, entry second — the confusability biconditional
# computes `backend_identical` off the resolved module, and against a placeholder CI stamps the wrong
# label). So the composer is driven against a synthetic spec that names it, which is enough: what is
# under test here is what COMPOSE does with a tagged-molecule layout, and none of it reads the KB for
# anything but the elements and the module id.


def _plate_spec() -> object:
    """A tagged-molecule chemistry on the plate module: 11 bp tag, 8 bp UMI, `GGG`, cDNA from 22.

    Built off a shipped bulk entry so every field this test does not care about is a real one, with
    the reads and the backend replaced. `model_copy` rather than `model_validate`, deliberately: the
    cell-axis biconditional is `tests/test_kb.py`'s subject and re-proving it here would make this
    fixture an assertion about a different rule.
    """
    from seqforge.kb.schema import Backend, Element, Read

    base = kb.load_spec("bulk-rnaseq")
    tagged = Read(
        id="R1",
        seqspec_read_id="read1",
        strand="pos",
        min_len=40,
        elements=[
            Element(
                type="fixed",
                name="tso_tag",
                start=0,
                end=11,
                sequence="ATTGCGCAATG",
                seqspec_region_type="custom_primer",
            ),
            Element(type="umi", name="umi", start=11, end=19, seqspec_region_type="umi"),
            Element(
                type="fixed",
                name="tso_ggg",
                start=19,
                end=22,
                sequence="GGG",
                seqspec_region_type="linker",
            ),
            Element(type="cdna", name="cdna", start=22, seqspec_region_type="cdna"),
        ],
    )
    plain = Read(
        id="R2",
        seqspec_read_id="read2",
        strand="neg",
        min_len=40,
        elements=[Element(type="cdna", name="cdna", start=0, seqspec_region_type="cdna")],
    )
    return base.model_copy(
        update={
            "identity": base.identity.model_copy(update={"sample_is_cell": True}),
            "reads": [tagged, plain],
            "read_sets": {},
            "backend": Backend(module="map/star-umi", params={}),
            "confusable_with": [],
        }
    )


def _plate_layout(manifest: DatasetManifest, *, tagged_first: bool = True) -> DatasetManifest:
    """The same manifest with a tagged-molecule read layout — optionally listing the PLAIN mate first.

    The order argument is the whole point of the fourth layout kind: `mates` would take these two by
    position, and they are not symmetric.
    """
    from seqforge.models.dataset import ReadDef, ReadElement, ReadLayout

    tagged = ReadDef(
        read_id="R1",
        strand="pos",
        min_len=40,
        max_len=150,
        elements=[
            ReadElement(
                role="linker",
                region_type="custom_primer",
                start=0,
                length=11,
                sequence="ATTGCGCAATG",
            ),
            ReadElement(role="UMI", region_type="umi", start=11, length=8),
            ReadElement(role="linker", region_type="linker", start=19, length=3, sequence="GGG"),
            ReadElement(role="cDNA", region_type="cdna", start=22),
        ],
    )
    plain = ReadDef(
        read_id="R2",
        strand="neg",
        min_len=40,
        max_len=150,
        elements=[ReadElement(role="cDNA", region_type="cdna", start=0)],
    )
    reads = [tagged, plain] if tagged_first else [plain, tagged]
    return _with_layout(manifest, ReadLayout(modality="rna", reads=reads))


def _with_layout(manifest: DatasetManifest, layout: object) -> DatasetManifest:
    """The same manifest carrying a different read layout, and nothing else changed."""
    return manifest.model_copy(
        update={"library": manifest.library.model_copy(update={"read_layout": layout})}
    )


@pytest.fixture
def plate(
    synth_bulk_pe: SynthDataset, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[DatasetManifest, ProcessingManifest]]:
    """A plate dataset + recipe, with the composer's spec lookup pointed at the synthetic chemistry."""
    from seqforge.compose import core as compose_core
    from seqforge.manifest.fill import ProcessingInputs, fill_processing

    spec = _plate_spec()
    monkeypatch.setattr(compose_core, "load_spec", lambda tech: spec)

    def build(*, tagged_first: bool = True) -> tuple[DatasetManifest, ProcessingManifest]:
        from seqforge import __version__

        manifest = _plate_layout(synth_bulk_pe.manifest, tagged_first=tagged_first)
        processing, _ = fill_processing(
            spec=spec,  # type: ignore[arg-type]
            dataset=manifest,
            processing=ProcessingInputs(assembly="sacCer3", annotation_name="ensembl"),
            processing_id="default",
            pin=True,
            seqforge_version=__version__,
        )
        return manifest, processing

    return build


def test_a_plate_composes_its_reads_by_role_whichever_order_the_layout_lists_them(
    plate: Callable[..., tuple[DatasetManifest, ProcessingManifest]],
    synth_bulk_pe: SynthDataset,
    tmp_path: Path,
) -> None:
    """The fourth layout kind picks the TAGGED read by role, and the order it is listed in is inert.

    This contrast is the entire reason `umi_tagged` exists rather than reusing `mates`. `mates` picks
    by ORDER, and a plate's two mates are not symmetric — one opens with tag + UMI + motif. A layout
    listing the plain mate first would hand the untagged read to the extractor: nothing is tagged, the
    count matrix is empty, and every exit code along the way is 0.

    So both orders are composed and the two configs must agree. One direction alone would pass under
    an order-based dispatch for whichever order happened to be written first.
    """
    for tagged_first in (True, False):
        manifest, processing = plate(tagged_first=tagged_first)
        result = compose(manifest, processing, registry=synth_bulk_pe.registry, workspace=tmp_path)
        assert result.modules[0].name == "map/star-umi"
        assert result.gate["params"] == "pass", result.params_preview["params_problems"]

        config = yaml.safe_load((tmp_path / result.config_path).read_text())
        assert config["read_files_in"] == {"umi_cdna": "R1", "cdna": "R2"}, (
            f"tagged_first={tagged_first} placed the roles by order, not by role"
        )


def test_a_plates_whole_extraction_geometry_arrives_as_one_derived_key(
    plate: Callable[..., tuple[DatasetManifest, ProcessingManifest]],
    synth_bulk_pe: SynthDataset,
    tmp_path: Path,
) -> None:
    """Six numbers, one key, and no owner but the element coordinates.

    The block this module reads is `umi:` and it carries exactly one thing. That it is DERIVED rather
    than declared is what makes the chemistry's parse namespace empty — there is nothing for a spec
    to write down and therefore nothing that can contradict the bytes.
    """
    manifest, processing = plate()
    config = plan(manifest, processing, registry=synth_bulk_pe.registry).config

    assert param_block_key(_plate_spec()) == "umi"  # type: ignore[arg-type]
    assert config["umi"] == {"read_structure": "R1:ATTGCGCAATG@0:umi@11+8:GGG@19:cdna@22"}
    # ...and it is owned by the ELEMENTS, not by the KB and not by the recipe. A plate recipe carries
    # no counting key at all: the counter writes all four matrices in one pass.
    assert param_owners(_plate_spec(), processing) == {"read_structure": "derived"}  # type: ignore[arg-type]


def test_the_params_gate_refuses_a_plate_wired_to_the_untagged_mate(
    plate: Callable[..., tuple[DatasetManifest, ProcessingManifest]],
    synth_bulk_pe: SynthDataset,
) -> None:
    """The load-bearing assertion for this pipeline, because the failure it catches exits 0.

    Handed the plain mate, the extractor finds no tag in any read, writes a uBAM with no `UB`
    anywhere, and every rule after it succeeds on an empty matrix. There is no non-zero exit and no
    error line to notice — which is why the composer's derivation is re-checked here rather than
    trusted, exactly as the two barcoded kinds are.
    """
    manifest, processing = plate()
    spec = _plate_spec()
    config = plan(manifest, processing, registry=synth_bulk_pe.registry).config
    assert params_gate(manifest, processing, spec, config) == ("pass", []), (  # type: ignore[arg-type]
        "the clean pair must pass"
    )

    swapped = {**config, "read_files_in": {"umi_cdna": "R2", "cdna": "R1"}}
    status, problems = params_gate(manifest, processing, spec, swapped)  # type: ignore[arg-type]
    assert status == "fail"
    assert any("is not the tagged read" in p for p in problems), problems


def test_a_plate_geometry_that_contradicts_the_observed_reads_is_refused(
    plate: Callable[..., tuple[DatasetManifest, ProcessingManifest]],
    synth_bulk_pe: SynthDataset,
) -> None:
    """The cross-derivation: what the chemistry declares and what the bytes are must be one geometry.

    This pipeline's whole config block is a single derived value, so there is no declared offset for
    a KB to get wrong — only the coordinates themselves. That makes this the one place the two
    element models are made to agree, and a disagreement means the extractor would cut a UMI out of
    bases the reads do not have.
    """
    manifest, processing = plate()
    spec = _plate_spec()
    config = plan(manifest, processing, registry=synth_bulk_pe.registry).config

    # The chemistry's elements say the UMI is 8 bp; these reads say 10, with everything after it
    # shifted so the layout stays a perfectly well-formed tagged read. That is the case worth
    # catching: a MALFORMED layout refuses on its own shape, and a plausible one does not.
    from seqforge.models.dataset import ReadElement

    layout = manifest.library.read_layout
    widened = layout.reads[0].model_copy(
        update={
            "elements": [
                layout.reads[0].elements[0],
                ReadElement(role="UMI", region_type="umi", start=11, length=10),
                ReadElement(
                    role="linker", region_type="linker", start=21, length=3, sequence="GGG"
                ),
                ReadElement(role="cDNA", region_type="cdna", start=24),
            ]
        }
    )
    contradicted = _with_layout(
        manifest, layout.model_copy(update={"reads": [widened, layout.reads[1]]})
    )

    status, problems = params_gate(contradicted, processing, spec, config)  # type: ignore[arg-type]
    assert status == "fail"
    assert any("derived from the observed read layout" in p for p in problems), problems


# ---- the plate gate: the shipped chemistry, planned, and then run at small N ---------------------
#
# Everything above drives a synthetic chemistry that names `map/star-umi`, because the module landed
# before any entry could name it. These drive the SHIPPED `smartseq3` entry, which is a different
# claim: that a plate deposit a user would actually resolve onto compiles, plans and runs.
#
# Three parts, and they share ONE `snakemake -n -p` (the `composed_plate` fixture):
#
#   wiring  -- the plan reaches every rule, including the shared load with its defensive removal,
#              and 96 cells' wildcards all resolve.
#   params  -- the extractor is handed the TAGGED mate, by role, and the geometry it is handed is
#              the published one. This is the load-bearing part: the failure it catches is a rule
#              wired to the plain mate, which finds no tag in any read, writes a uBAM with no `UB`
#              anywhere, and finishes on an empty matrix at exit 0.
#   e2e     -- eight cells, a tiny synthetic reference, and the composed pipeline's OWN rendered
#              command lines executed against reads whose every count is known by construction.
#
# `--lint` is deliberately not part of the wiring half. It was in the shipped wiring gate and was
# removed on measurement: it is red on every rule this repo ships, for a missing `log:` directive and
# for "mixed rules and functions in same snakefile" -- style opinions, not wiring facts. Adding it
# back here would make this gate red for a correct pipeline, which is how a gate comes to be ignored.
#
# THE STRAND-SENSITIVITY CLAUSE HAS NO ANALOGUE HERE, AND ITS ABSENCE IS RECORDED RATHER THAN LEFT
# TO BE NOTICED. Every other end-to-end gate in this repo asserts that inverting the counter's strand
# setting moves the answer, which is what proves the shipped setting was a decision rather than a
# default nobody looked at. This counter is UNSTRANDED and has no knob: it builds its feature index
# unstranded for tagged and internal reads alike, the chemistry's backend params are empty, and the
# published reference configuration for this assay is unstranded too. There is no value to invert, so
# there is no experiment to run -- and a gate that quietly carried one clause fewer than its
# siblings would read as an oversight rather than as the finding it is.


def _units_by_read(pipeline_dir: Path) -> dict[tuple[str, str], str]:
    """``(sample_id, read_id) -> path``, straight off the emitted units table.

    The read ids come from the composer's own grouping rather than from a filename convention this
    file re-invents: which file is the tagged one is a claim the composer makes, and a test that
    guessed it from the name could not catch the composer getting it wrong.
    """
    import csv

    with (pipeline_dir / "units.tsv").open(newline="") as fh:
        return {
            (row["sample_id"], row["read_id"]): row["path"]
            for row in csv.DictReader(fh, delimiter="\t")
        }


def _rendered_shell(plan_text: str) -> dict[str, dict[str, str]]:
    """``rule -> wildcard value -> the shell command snakemake rendered for it``.

    `-p` is what makes this readable at all: without it a dry run never formats a `shell:` block, so
    a param the command dereferences and the config does not carry plans clean and dies on a compute
    node. A rule with no wildcards is keyed under the empty string.
    """
    jobs: dict[str, dict[str, str]] = {}
    rule = wildcard = None
    lines = plan_text.splitlines()
    for i, line in enumerate(lines):
        if match := re.match(r"^rule (\w+):$", line):
            rule, wildcard = match.group(1), ""
        elif match := re.match(r"^\s+wildcards: sample=(\S+)$", line):
            wildcard = match.group(1)
        elif line.rstrip() == "Shell command:" and rule is not None:
            body: list[str] = []
            for following in lines[i + 1 :]:
                if not following.strip():
                    break
                body.append(following)
            jobs.setdefault(rule, {})[wildcard or ""] = "\n".join(body)
    return jobs


@pytest.mark.xdist_group("composed-plate")
def test_a_composed_plate_plans_every_rule_and_resolves_every_cells_wildcard(
    composed_plate: ComposedPlate,
) -> None:
    """Part one: the plan a real plate deposit produces, at a cell count a plate actually has.

    Four claims a dry run is the only thing that can make. That the plan REACHES every rule, so no
    rule is unreachable from `rule all`. That the per-cell chain expands once per cell while the
    fan-in stays ONE job — the ratio is this module's whole shape, and a per-cell counter followed
    by a merge would read as 96 here. That every cell's deliverable is demanded by NAME, since a
    rule whose output is a folder is satisfied by a folder, which is how a counting job that wrote
    three cells of 1440 exits 0. And that the shared index is loaded once and attached 96 times,
    which is the arithmetic the whole module exists for.

    It opens with compose's own `wiring` verdict, which is the same four characters
    `test_a_single_end_plate_deposit_compiles_end_to_end` asserts for the other placement shape. The
    claims below read a plan text this test spawned for itself; the verdict reads the gate `compose`
    RAN, and a verdict is what ADR-0035's universal — "no path exists where `compose` exits 0 and the
    module then raises" — is stated in. `_role_placement`'s `umi_tagged` branch emits exactly two
    shapes, so the two assertions together are the whole case analysis rather than two samples of it.
    """
    plan_text = composed_plate.plan_text
    assert composed_plate.gate["wiring"] == "pass", (
        f"the `{{umi_cdna, cdna}}` placement did not reach the DAG builder: {composed_plate.gate}"
    )
    assert (composed_plate.pipeline_dir / "star-umi.smk").is_file(), (
        "compose copied no plate module beside its wrapper"
    )

    for rule, jobs in (
        ("genome_index", 1),
        ("load_genome", 1),
        ("umi_extract", PLATE_CELL_COUNT),
        ("star_umi_map", PLATE_CELL_COUNT),
        ("umi_to_cram", PLATE_CELL_COUNT),
        ("umi_count", 1),
    ):
        assert re.search(rf"^{rule}\s+{jobs}\s*$", plan_text, re.M), (
            f"the plan does not schedule `{rule}` exactly {jobs} time(s)"
        )

    # Every cell's wildcard resolves, not merely the first — an expansion that dropped cells would
    # still plan a coherent DAG over the ones it kept.
    outdir = composed_plate.config["outdir"]
    assert f"{outdir}/{PLATE_H5AD}" in plan_text
    missing = [c for c in composed_plate.cells if f"{outdir}/{c}/{c}.cram" not in plan_text]
    assert not missing, f"{len(missing)} cells never reached a CRAM target: {missing[:5]}"

    # The shared-index contract, rendered rather than merely written: the load marks any stale
    # segment for destruction before loading, and every mapping job ATTACHES instead of loading.
    rendered = _rendered_shell(plan_text)
    load = rendered["load_genome"][""]
    assert "--genomeLoad Remove" in load and "--genomeLoad LoadAndExit" in load
    assert "|| true" in load, "removing a segment that is not there is a STAR error and a no-op"
    maps = rendered["star_umi_map"]
    assert len(maps) == PLATE_CELL_COUNT
    assert all("--genomeLoad LoadAndKeep" in cmd for cmd in maps.values())

    # ...and the release runs on BOTH paths. A dry run fires no handler and this suite owns no
    # scheduler to kill a job on, so the two handlers are read off the module compose EMITTED —
    # which is a different file from the one in `src/`, and the one this pipeline would actually run.
    # Each handler calls one helper; the command itself is written once, just above them.
    emitted = (composed_plate.pipeline_dir / "star-umi.smk").read_text()
    for handler in ("onsuccess:", "onerror:"):
        body = emitted.split(handler, 1)[1] if handler in emitted else ""
        assert body, f"the emitted module carries no `{handler}` handler"
        assert "release_genome_segment()" in body.split("\n\n", 1)[0]
    helper = emitted[emitted.index("def release_genome_segment(") : emitted.index("\nonsuccess:")]
    assert "--genomeLoad Remove" in helper and "|| true" in helper, helper


@pytest.mark.xdist_group("composed-plate")
def test_the_composed_plate_hands_the_extractor_the_tagged_mate_and_never_the_plain_one(
    composed_plate: ComposedPlate,
) -> None:
    """Part two, the half a dry run can see: which FILE the extractor is actually handed.

    The config-level claim — that `read_files_in` is `{umi_cdna, cdna}` chosen by role and not by
    the order the layout lists them — is already pinned against a synthetic chemistry above. What
    this adds is the rest of the journey: role -> read id -> the units table -> the argument a
    rendered command line carries, for every cell. Nothing between compose and the extractor may
    quietly re-pair them, and the failure if something does is a full run at 0% UMI yield and exit 0.
    """
    geometry = TagGeometry.parse(str(composed_plate.config["umi"]["read_structure"]))  # type: ignore[index]
    roles = composed_plate.config["read_files_in"]
    assert isinstance(roles, dict)
    assert roles["umi_cdna"] == geometry.read_id, (
        "the read the extractor is pointed at and the read the geometry describes are not the same"
    )

    # The tagged read is the one the LAYOUT says carries a UMI, so "tagged" is read off the element
    # model rather than off a read id that happens to be spelled R1.
    layout = composed_plate.manifest.library.read_layout
    tagged = [r.read_id for r in layout.reads if any(e.role == "UMI" for e in r.elements)]
    assert tagged == [geometry.read_id], (
        f"the layout's tagged read is {tagged}, not {geometry.read_id}"
    )

    units = _units_by_read(composed_plate.pipeline_dir)
    extractions = _rendered_shell(composed_plate.plan_text)["umi_extract"]
    assert len(extractions) == PLATE_CELL_COUNT
    for cell, command in extractions.items():
        argv = re.search(r"--r1 (\S+) --r2 (\S+)", command)
        assert argv, f"the extractor's rendered command names no mates:\n{command}"
        assert argv.group(1) == units[cell, roles["umi_cdna"]]
        assert argv.group(2) == units[cell, roles["cdna"]]
        assert f"--read-id {geometry.read_id}" in command
        # ...and the plain mate is nowhere near `--r1`, which is the swap that exits 0.
        assert argv.group(1) != units[cell, roles["cdna"]]


@pytest.mark.xdist_group("composed-plate")
def test_the_composed_plate_derives_the_offsets_the_protocol_published(
    composed_plate: ComposedPlate,
) -> None:
    """Part two, the half that catches an offset off by one — and nothing else can.

    The chemistry declares no geometry at all: its backend params are empty and every knob is
    derived from the element coordinates, so there is no declared number for a KB review to read
    and disagree with. That is what makes the emitted offsets worth asserting against the numbers
    the assay's own authors published rather than against the elements they were derived from —
    a test that re-derived them from the same coordinates could not fail.

    The published configuration for this assay states, in ONE-BASED inclusive coordinates: the tag
    `ATTGCGCAATG` at 1-11, the UMI at 12-19, and cDNA from 23. The three bases between are the `GGG`
    the template-switch oligo ends on. Everything below converts what compose emitted into those
    coordinates and compares.
    """
    spec = kb.load_spec("smartseq3")
    assert spec.require_backend().params == {}, (
        "a plate chemistry that declares a parse key has taken ownership of a derived one"
    )
    assert param_block_key(spec) == "umi"
    assert param_owners(spec, composed_plate.processing) == {"read_structure": "derived"}

    umi = composed_plate.config["umi"]
    assert isinstance(umi, dict)
    assert set(umi) == {"read_structure"}, "the whole extraction geometry is ONE key, or it is six"
    geometry = TagGeometry.parse(str(umi["read_structure"]))

    def one_based(offset: int) -> int:
        """A 0-based half-open start, as the protocol's own inclusive coordinate."""
        return geometry.anchor_start + offset + 1

    assert geometry.anchor == "ATTGCGCAATG"
    assert (one_based(0), one_based(len(geometry.anchor) - 1)) == (1, 11)
    assert (
        one_based(geometry.umi_offset),
        one_based(geometry.umi_offset + geometry.umi_length - 1),
    ) == (12, 19)
    assert (
        one_based(geometry.trailing_offset),
        one_based(geometry.trailing_offset + len(geometry.trailing) - 1),
    ) == (20, 22)
    assert geometry.trailing == "GGG"
    assert one_based(geometry.cdna_offset) == 23
    # ...and the rendered value is what the rule actually hands over, not a value this test rebuilt.
    assert f"--geometry {geometry.render()}" in composed_plate.plan_text


# ---- part three: eight cells, a tiny reference, and the pipeline's OWN rendered commands ---------
#
# The synthetic annotation and BAM in `test_workflows.py` are the better proof of the counting RULE:
# every fragment's fate is known by construction there, with no aligner in the way. What a hand-made
# BAM cannot catch is compose deriving a cDNA start of 23 where the protocol says 22, because a
# hand-made BAM starts after the geometry has already been applied. This run starts before it — the
# reads below are built from the PUBLISHED protocol and cut with the COMPOSED geometry, so the two
# have to agree or the alignment lands one base off its injected position.
#
# No species: the reference is 24 kb of seeded random sequence with three genes written over it. A
# real-genome slice would bake one assembly and one annotation into this repo for a claim that has
# nothing to do with either.

#: The tag the protocol publishes, spelled out here rather than read from the chemistry. This file's
#: whole reason for building reads by hand is that a read built from the emitted geometry is
#: self-consistent with a wrong emitted geometry.
_SS3_TAG, _SS3_TRAILING, _SS3_UMI_LEN = "ATTGCGCAATG", "GGG", 8

_E2E_CELLS = 8
_E2E_CONTIG_LEN = 24_000
_E2E_SEED = 295
_E2E_FRAGMENT, _E2E_READ_LEN = 200, 50

#: 1-based inclusive, as a GTF states them: one long exon each, far enough apart that no 200 bp
#: fragment placed inside one can reach another. Ambiguity has its own coverage against the
#: synthetic BAM; mixing it in here would make the loss clause unreadable.
_E2E_GENES = (("GENE_A", 2001, 3500), ("GENE_B", 8001, 9500), ("GENE_C", 14001, 15500))

#: A 400 bp block of GENE_A's exon, copied verbatim to a stretch no gene covers (0-based, half-open
#: for the source). A fragment falling entirely inside it aligns to two loci, so the ALIGNER tags it
#: `NH:i:2` — which is the only way to obtain a real multimapper rather than to assert one into a
#: BAM this file wrote.
_E2E_DUP_SRC, _E2E_DUP_DST = (2400, 2800), 20_000

#: Where each cell's fragments start, 0-based. Everything under `_E2E_UNIQUE` is a distinct UMI in
#: one gene's exon and clear of the duplicated block.
_E2E_UNIQUE: dict[str, tuple[int, ...]] = {
    "GENE_A": (2900, 2950, 3000, 3050, 3100, 3150),
    "GENE_B": (8100, 8200, 8300, 8400, 8500),
    "GENE_C": (14100, 14200, 14300, 14400),
}
#: Inside the duplicated block, so both mates land there and the aligner reports two loci.
_E2E_MULTIMAPPER_STARTS = (2450, 2500)
#: No tag at all — the untagged population is 32-68% of a real library of this chemistry. They must
#: reach the READ matrices and leave the UMI matrix alone.
_E2E_UNTAGGED_STARTS = (9000, 9100)
#: One more GENE_A fragment carrying a UMI already used, so the count is distinct UMIs and not reads.
_E2E_REPEATED_UMI_START = 3200

#: What the cells above must come to, per cell, in the primary (exonic UMI) matrix.
_E2E_TRUTH_PER_CELL = {gene: len(starts) for gene, starts in _E2E_UNIQUE.items()}


def _e2e_umis(n: int) -> list[str]:
    """``n`` UMIs pairwise further apart than the corrector's Hamming-1 merge can reach.

    Random 8-mers would occasionally land one error apart, and the corrector would then absorb the
    rarer of the two into the commoner — a real and correct behaviour that would show up here as an
    unexplained loss and indict the pipeline for the fixture's carelessness.
    """
    rng = random.Random(_E2E_SEED)
    pool: list[str] = []
    while len(pool) < n:
        umi = "".join(rng.choice("ACGT") for _ in range(_SS3_UMI_LEN))
        if all(sum(a != b for a, b in zip(umi, other, strict=True)) > 2 for other in pool):
            pool.append(umi)
    return pool


def _e2e_contig() -> str:
    """24 kb of seeded random sequence, with one 400 bp block appearing twice."""
    rng = random.Random(_E2E_SEED)
    bases = [rng.choice("ACGT") for _ in range(_E2E_CONTIG_LEN)]
    bases[_E2E_DUP_DST : _E2E_DUP_DST + (_E2E_DUP_SRC[1] - _E2E_DUP_SRC[0])] = bases[
        _E2E_DUP_SRC[0] : _E2E_DUP_SRC[1]
    ]
    return "".join(bases)


def _revcomp(seq: str) -> str:
    """The reverse complement, so the second mate maps in the orientation a pair maps in."""
    return seq.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def _e2e_pair(contig: str, start: int, umi: str | None) -> tuple[str, str]:
    """One fragment as the two reads a sequencer would have written for it.

    The tagged read is assembled from the PROTOCOL's own pieces — tag, UMI, trailing motif, cDNA —
    and never from the geometry under test, which is what leaves the composed offsets somewhere to
    be wrong.
    """
    cdna = contig[start : start + _E2E_READ_LEN]
    mate = _revcomp(contig[start + _E2E_FRAGMENT - _E2E_READ_LEN : start + _E2E_FRAGMENT])
    if umi is None:
        return cdna, mate
    return _SS3_TAG + umi + _SS3_TRAILING + cdna, mate


def _e2e_cell(contig: str) -> tuple[list[str], list[str], list[int]]:
    """One cell's two FASTQ read lists, and the reference positions its tagged reads were cut to."""
    umis = _e2e_umis(sum(len(s) for s in _E2E_UNIQUE.values()) + len(_E2E_MULTIMAPPER_STARTS))
    placed: list[tuple[int, str | None]] = []
    for starts in _E2E_UNIQUE.values():
        placed += [(start, umis.pop()) for start in starts]
    first_gene_umi = placed[0][1]
    placed.append((_E2E_REPEATED_UMI_START, first_gene_umi))
    placed += [(start, umis.pop()) for start in _E2E_MULTIMAPPER_STARTS]
    placed += [(start, None) for start in _E2E_UNTAGGED_STARTS]

    r1, r2 = [], []
    for start, umi in placed:
        tagged, mate = _e2e_pair(contig, start, umi)
        r1.append(tagged)
        r2.append(mate)
    return r1, r2, [start for start, umi in placed if umi is not None]


def _e2e_annotation(directory: Path) -> Path:
    """The three-gene GTF, built into the database liulab-genome would have built from it.

    The flags mirror the registrar's — inference disabled because a real annotation declares its own
    `gene` rows — so what the counter opens here is the same kind of object it opens on a cluster.
    """
    import gffutils

    lines = []
    for gene, start, end in _E2E_GENES:
        lines.append(
            f'chr1\tsynthetic\tgene\t{start}\t{end}\t.\t+\t.\tgene_id "{gene}"; gene_name "{gene.lower()}";'
        )
        lines.append(
            f'chr1\tsynthetic\texon\t{start}\t{end}\t.\t+\t.\tgene_id "{gene}"; transcript_id "{gene}.1";'
        )
    gtf = directory / "synthetic.gtf"
    gtf.write_text("\n".join(lines) + "\n")
    db = directory / "synthetic.db"
    built = gffutils.create_db(
        str(gtf),
        str(db),
        keep_order=True,
        merge_strategy="create_unique",
        sort_attribute_values=True,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )
    built.conn.close()
    return db


def _e2e_shell(command: str, work: Path) -> None:
    """Run one of the pipeline's OWN rendered shell blocks, in ``work``, and refuse on a failure.

    `shell=True` deliberately: what is being executed is a `shell:` block, line continuations,
    comments, `|| true` and all, exactly as snakemake would hand it to `/bin/bash`. Re-tokenising it
    into an argv would be this file writing a second command line, which is the one thing an
    end-to-end gate over a composed pipeline must not do.
    """
    proc = subprocess.run(  # noqa: S602
        command, shell=True, cwd=work, capture_output=True, text=True, executable="/bin/bash"
    )
    assert proc.returncode == 0, (
        f"{command}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


@pytest.mark.external
@pytest.mark.xdist_group("composed-plate")
def test_a_composed_plate_runs_end_to_end_at_small_n_and_recovers_its_injected_counts(
    composed_plate: ComposedPlate, tmp_path: Path
) -> None:
    """Eight cells, start to finish, through the command lines compose actually emitted.

    Five clauses, and each is here because something specific would otherwise be silent:

    * **no spurious pair** — a count in a cell/gene nothing was injected into is fabrication, and
      fabrication is the failure a corpus cannot recover from.
    * **no inflated count** — the untagged reads and the multimappers are both built to be excluded,
      so either leaking into the primary matrix shows up here and nowhere else.
    * **unexplained loss at most 2%** — a pipeline that drops reads quietly is a pipeline whose
      matrices are plausible and small.
    * **determinism** — count the same plate twice and the bytes must be identical. It earns its
      place for a sharp reason: the engine this counter was ported from resolved a read with several
      primary alignments by an unseeded random choice, and that default was found live in it.
    * **multimappers reported, not inferred** — the mapping rule writes ONE alignment per read, so a
      counter that inferred multimapping from how many records a read produced would report zero of
      them and fold every one into a gene. This counter reads `NH`, and the clause below turns that
      claim into a measurement by checking the aligner's own tag against what the object reports.

    The uBAM assertions are the part that owns the derived geometry, and they are the reason this
    run starts from FASTQ rather than from a BAM somebody wrote. The reads are assembled from the
    published protocol and cut with the composed offsets, so a cDNA start one base off its published
    value leaves the uBAM carrying 49 genomic bases beginning one base late — which is checked here
    against the contig those bases were taken from, before an aligner has had a chance to absorb it
    into a soft clip and finish at exit 0. A hand-made BAM begins after that cut has happened.
    """
    import anndata as ad
    import pysam

    from seqforge.workflows.umite.count import PRIMARY_MATRIX, write_umi_counts

    star, samtools = shutil.which("STAR"), shutil.which("samtools")
    if star is None or samtools is None:
        pytest.skip("needs STAR and samtools on PATH (liulab-runtime's align-rna env)")

    rendered = _rendered_shell(composed_plate.plan_text)
    cells = sorted(rendered["star_umi_map"])[:_E2E_CELLS]
    units = _units_by_read(composed_plate.pipeline_dir)
    roles = composed_plate.config["read_files_in"]
    assert isinstance(roles, dict)

    work = tmp_path / "run"
    work.mkdir()
    contig = _e2e_contig()
    (work / "genome.fa").write_text(f">chr1\n{contig}\n")
    index = Path(str(composed_plate.config["outdir"])) / "index"
    (work / index).mkdir(parents=True)
    genome_dir = re.search(r"--genomeDir (\S+)", rendered["load_genome"][""])
    assert genome_dir, "the load rule renders no --genomeDir to build an index at"
    subprocess.run(
        [star, "--runMode", "genomeGenerate", "--genomeDir", genome_dir.group(1),
         "--genomeFastaFiles", "genome.fa", "--genomeSAindexNbases", "6",
         "--outFileNamePrefix", str(work / "gg_")],
        check=True, capture_output=True, cwd=work,
    )  # fmt: skip

    truth: dict[tuple[str, str], int] = {}
    injected_starts: dict[str, list[int]] = {}
    for cell in cells:
        r1, r2, starts = _e2e_cell(contig)
        write_fastq_gz(work / units[cell, roles["umi_cdna"]], r1, prefix=f"{cell}:read")
        write_fastq_gz(work / units[cell, roles["cdna"]], r2, prefix=f"{cell}:read")
        injected_starts[cell] = starts
        for gene, count in _E2E_TRUTH_PER_CELL.items():
            truth[cell, gene] = count

    try:
        _e2e_shell(rendered["load_genome"][""], work)
        for cell in cells:
            _e2e_shell(rendered["umi_extract"][cell], work)
            # The uBAM is where the derived geometry becomes bases. Every tagged read must carry the
            # UMI that was injected into it and exactly the 50 genomic bases it was built from — one
            # base of slack either way is a cDNA start that is not the published one.
            ubam = work / re.search(r"--out (\S+)", rendered["umi_extract"][cell]).group(1)  # type: ignore[union-attr]
            with pysam.AlignmentFile(str(ubam), "rb", check_sq=False) as unaligned:
                tagged = [
                    record
                    for record in unaligned.fetch(until_eof=True)
                    if record.is_read1 and record.has_tag("UB")
                ]
            assert len(tagged) == len(injected_starts[cell])
            assert {str(r.query_sequence) for r in tagged} == {
                contig[start : start + _E2E_READ_LEN] for start in injected_starts[cell]
            }, "the extractor cut cDNA at an offset the protocol does not publish"
            _e2e_shell(rendered["star_umi_map"][cell], work)
    finally:
        subprocess.run(
            [star, "--genomeDir", genome_dir.group(1), "--genomeLoad", "Remove",
             "--outFileNamePrefix", str(work / "unload_")],
            capture_output=True, cwd=work,
        )  # fmt: skip

    bams = {
        cell: work
        / re.search(r"--outFileNamePrefix (\S+)", rendered["star_umi_map"][cell]).group(1)  # type: ignore[union-attr]
        / "Aligned.sortedByCoord.out.bam"
        for cell in cells
    }
    # The aligner's own answer, before the counter has read anything: with one alignment written per
    # read, `NH` is the ONLY surviving evidence that a read had somewhere else to go.
    multimapped: dict[str, int] = {}
    for cell, bam in bams.items():
        with pysam.AlignmentFile(str(bam), "rb") as aligned:
            names = {
                str(r.query_name) for r in aligned.fetch(until_eof=True) if int(r.get_tag("NH")) > 1
            }
        multimapped[cell] = len(names)
    assert all(n == len(_E2E_MULTIMAPPER_STARTS) for n in multimapped.values()), multimapped
    assert sum(multimapped.values()) > 0, (
        "nothing in this plate aligned to two loci, so the multimapper clause measured nothing — "
        "the duplicated block the fixture writes into the contig is what makes it a measurement"
    )

    db = _e2e_annotation(tmp_path)
    plate = [(cell, bams[cell]) for cell in cells]
    first = write_umi_counts(plate, db, tmp_path / "first.h5ad")
    second = write_umi_counts(plate, db, tmp_path / "second.h5ad")
    assert first.read_bytes() == second.read_bytes(), (
        "counting the same plate twice moved the bytes"
    )

    adata = ad.read_h5ad(first)
    matrix = count_matrix(adata).toarray()
    observed = {
        (str(cell), str(gene)): int(matrix[i, j])
        for i, cell in enumerate(adata.obs_names)
        for j, gene in enumerate(adata.var_names)
        if matrix[i, j]
    }
    assert adata.uns["primary_matrix"] == PRIMARY_MATRIX

    spurious = {key: n for key, n in observed.items() if key not in truth}
    inflated = {key: (n, truth[key]) for key, n in observed.items() if n > truth.get(key, 0)}
    recovered = sum(min(n, truth.get(key, 0)) for key, n in observed.items())
    assert spurious == {}, (
        f"counts appeared in cell/gene pairs nothing was injected into: {spurious}"
    )
    assert inflated == {}, f"counts exceeded what was injected (observed, injected): {inflated}"
    loss = 1 - recovered / sum(truth.values())
    assert loss <= 0.02, f"{loss:.1%} of injected UMIs never reached the matrix"

    # The multimapper clause, closed: the aligner said `NH > 1` for two reads a cell, the object
    # reports exactly those two, and none of them was folded into GENE_A's exon on the way.
    assert dict(zip(adata.obs_names, adata.obs["multimapping"], strict=True)) == {
        cell: multimapped[cell] for cell in cells
    }
    # ...and the untagged pairs landed in the READ matrix instead of inflating the UMI one.
    reads = count_matrix(adata, "read_exon").toarray()
    gene_b = adata.var_names.get_loc("GENE_B")
    assert {int(reads[i, gene_b]) for i in range(len(cells))} == {len(_E2E_UNTAGGED_STARTS)}


# ---- the OTHER placement shape: a plate sequenced single-end, and a gate proved to be looking -----
#
# `_role_placement`'s `umi_tagged` branch emits exactly two shapes, `{umi_cdna, cdna}` and
# `{umi_cdna}`, so ADR-0035's universal — "no path exists where `compose` exits 0 and the module then
# raises" — is a FINITE case analysis. The paired plate above is the first shape; these are the
# second, on the same shipped `smartseq3` entry, whose `read_sets: {se: [R1]}` is what makes the
# mate-less placement reachable at all.
#
# `gate["wiring"] == "pass"` is the assertion that does the work in both, and nothing cheaper can
# stand in for it: that gate IS `snakemake -n -p`, i.e. the DAG BUILDER, which is exactly where the
# deleted raise used to land (`InputFunctionException … ValueError: this layout carries only the
# tagged read`, measured at 7e2488f). The placement itself was always emittable — the composer has
# been able to produce a mate-less one since it was written — so a config-level assertion proves only
# that compose tolerated a one-read layout, never that the module can plan it.


def test_a_single_end_plate_deposit_compiles_end_to_end(
    synth_plate_se: SynthDataset, tmp_path: Path, real_wiring_gate: None
) -> None:
    """A plate sequenced single-end, from one FASTQ to a Snakefile: resolve -> fill -> compose -> plan.

    The plate half of `test_a_single_end_bulk_deposit_compiles_end_to_end`, and every claim is one the
    BYTE RESOLVER made rather than one a fixture trimmed: the deposit is a single file, so the
    one-read layout, the one-file inventory and the `{umi_cdna}` placement all follow from the
    resolver having selected `smartseq3`'s `se` read set.

    **Asserting the chemistry is not scene-setting.** On one file `smartseq3/se` and `bulk-rnaseq/se`
    score inside the tie band, so the metadata assertion the fixture supplies is what lands this on a
    plate rather than on generic bulk — and generic bulk here is a gene-count matrix for a plate
    library at exit 0, which `docs/agents/kb.md` ranks as the worst outcome available. See
    :data:`conftest.synth_plate_se` for the measured margin.

    **`gate["wiring"] == "pass"` is the point of the whole test.** Everything above it is text off
    disk and would hold just as well of a composer that emitted a placement the module goes on to
    raise over — which is precisely the state ADR-0035 removes, and the state the shipped `se` read
    set would otherwise have unlocked. That gate spawns `snakemake -n -p`, so it is the DAG builder's
    own answer to "can this pipeline be planned at all", taken over the mate-less shape. It is the
    difference between "compose tolerated a one-read layout" and "the module can actually plan it".
    """
    manifest, reg = synth_plate_se.manifest, synth_plate_se.registry
    assert manifest.library.chemistry.value == ["smartseq3"]
    assert [r.read_id for r in manifest.library.read_layout.reads] == ["R1"]
    assert [f.read_id for f in manifest.library.files] == ["R1"]

    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star-umi"
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["read_files_in"] == {"umi_cdna": "R1"}, "the mate-less placement was not emitted"
    assert result.gate["params"] == "pass", result.params_preview["params_problems"]
    assert result.gate["wiring"] == "pass", (
        f"the mate-less placement never reached a runnable DAG: {result.gate}"
    )
    assert (tmp_path / result.snakefile_path).is_file()  # the deliverable a user submits

    units = (tmp_path / result.units_path).read_text().splitlines()
    assert units[0].split("\t") == ["sample_id", "run", "lane", "read_id", "path"]
    assert len(units) == 1 + len(manifest.experiment.samples), (
        f"one read per cell -> header + one row per cell; got {units}"
    )
    assert {row.split("\t")[3] for row in units[1:]} == {"R1"}


def test_a_plate_the_dag_builder_cannot_plan_would_be_caught(
    synth_plate_se: SynthDataset, tmp_path: Path, real_wiring_gate: None
) -> None:
    """Prove the wiring verdict above is LOOKING: break the plate, and it must go red.

    A universal asserted only by a green verdict is a universal nobody has tested — and this one has
    two ways to be green for the wrong reason. `wiring_gate` returns `"skip"` where `snakemake` is
    absent, and conftest stubs it to the literal `"skip"` for every test that does not ask for the
    real one, so `!= "fail"` or a truthiness check would hold on a machine that planned nothing at
    all. Both assertions here are therefore for an EXACT verdict, in both directions.

    The break is chosen to land where the deleted raise landed, and it does not depend on that raise:
    the emitted config loses `read_files_in.umi_cdna`, so `tagged_role()` raises `KeyError` inside
    `rule umi_extract`'s input function and snakemake reports `InputFunctionException … Building DAG
    of jobs...` at exit 1 — the same class, at the same point, as the ADR's measurement at 7e2488f.
    The mate-less half of that config is left exactly as compose emitted it, so what is under test is
    the gate's reach and not the layout.
    """
    from seqforge.compose.gates import wiring_gate

    manifest, reg = synth_plate_se.manifest, synth_plate_se.registry
    processing = _processing(manifest)
    result = compose(manifest, processing, registry=reg, workspace=tmp_path, run_wiring_gate=False)
    pipeline_dir = (tmp_path / result.snakefile_path).parent
    p = plan(manifest, processing, registry=reg)
    assert wiring_gate(pipeline_dir, p) == "pass", (
        "the unbroken single-end plate must plan, or the red below says nothing"
    )

    config_path = pipeline_dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    del config["read_files_in"]["umi_cdna"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=True))
    assert wiring_gate(pipeline_dir, p) == "fail", (
        "a plate whose tagged read the module cannot name dies while the DAG is being built, and "
        "the gate called it a pass"
    )


# ---- ...and those eight cells RUN, which is the leg ADR-0035 argued instead of measuring ---------
#
# Everything above this line stops at `snakemake -n`, and a plan is not a matrix. The record's own
# standard is "measured, not read", and the one clause it does not meet is its own: "Nothing changes
# in the counter. It already treats an unpaired record as its own fragment." Nothing in this suite
# had ever executed STAR over a mate-less uBAM or run the counter over unpaired alignments, so that
# sentence was an argument about two functions read side by side. Below it is a number.


@pytest.mark.external
@pytest.mark.xdist_group("composed-plate-se")
def test_a_composed_single_end_plate_runs_end_to_end_and_recovers_its_injected_counts(
    composed_plate_se: ComposedPlate, tmp_path: Path
) -> None:
    """Eight cells with NO mate anywhere, start to finish, through the commands compose emitted.

    The sibling of `test_a_composed_plate_runs_end_to_end_at_small_n_and_recovers_its_injected_counts`
    over the other placement `_role_placement` can emit, and it reads the same fixture reads: same
    contig, same UMIs, same injected positions, same truth table, with the mate simply not written.
    That is the whole difference and it is the point — a difference in the answer is the mate's
    absence and nothing else.

    The silent failure it catches is the one no dry run can: a single-end plate that plans, aligns
    and exits 0 on an EMPTY or HALVED primary matrix. The extractor writing a record that keeps the
    PAIRED bit, the aligner reading unpaired records as pairs, or the counter's representative rule
    dropping every fragment whose `is_read1` bit was never set — the first two are loud, the third is
    a plausible small object, and only a run tells them apart.

    **Where the paired sibling speaks about mate 2 this either drops the clause or asserts its
    opposite, and which is which is recorded rather than left to be inferred:**

    * `--r2` — ASSERTS THE OPPOSITE. The rendered extraction must carry no `--r2` at all, rather
      than one with nothing after it: a `shell:` block is a static string, so an empty argument
      would swallow the flag behind it and fail as a usage error at job time.
    * the uBAM's `is_read1` filter — ASSERTS THE OPPOSITE. The paired run picks each fragment's
      tagged record out by `is_read1`; here NOTHING may be flagged PAIRED and the file must hold one
      record per fragment rather than two. `--readFilesType` is derived from exactly that, and a
      record that kept PAIRED with no mate beside it reads back as a truncated pair: `FATAL ERROR in
      input BAM file: the consecutive lines in paired-end BAM have different read IDs`, exit 104.
    * `_e2e_pair`'s second return value — DROPPED. The mate is still built and then thrown away, so
      both tests write their tagged FASTQ from the same call and neither can drift from the other.
    * the `TLEN`-derived fragment span — DROPPED, with nothing put in its place. `_fragment_span`
      falls back to an unpaired record's own footprint, which is 50 bases here rather than 200; the
      genes are 1 500 bp and every injected start sits well inside one, so the assignment is
      unchanged and the truth table is the paired one, unmodified.

    **And one clause the paired run cannot make: the accounting CLOSES.** One read is one fragment
    here, so every record that went in has to come out somewhere nameable — a distinct UMI in the
    primary matrix, an internal read in `read_exon`, a multimapper, or the single repeated UMI that
    collapses into the one it repeats. A pipeline that quietly counted half of a stream with no
    halves still produces a matrix that passes every clause above; the sum is what says otherwise.
    """
    import anndata as ad
    import pysam

    from seqforge.workflows.umite.count import N_FRAGMENTS, PRIMARY_MATRIX, write_umi_counts

    star, samtools = shutil.which("STAR"), shutil.which("samtools")
    if star is None or samtools is None:
        pytest.skip("needs STAR and samtools on PATH (liulab-runtime's align-rna env)")

    rendered = _rendered_shell(composed_plate_se.plan_text)
    cells = sorted(rendered["star_umi_map"])[:_E2E_CELLS]
    units = _units_by_read(composed_plate_se.pipeline_dir)
    roles = composed_plate_se.config["read_files_in"]
    assert isinstance(roles, dict)
    assert set(roles) == {"umi_cdna"}, f"this is the mate-less placement, and it carries {roles}"

    work = tmp_path / "run"
    work.mkdir()
    contig = _e2e_contig()
    (work / "genome.fa").write_text(f">chr1\n{contig}\n")
    index = Path(str(composed_plate_se.config["outdir"])) / "index"
    (work / index).mkdir(parents=True)
    genome_dir = re.search(r"--genomeDir (\S+)", rendered["load_genome"][""])
    assert genome_dir, "the load rule renders no --genomeDir to build an index at"
    subprocess.run(
        [star, "--runMode", "genomeGenerate", "--genomeDir", genome_dir.group(1),
         "--genomeFastaFiles", "genome.fa", "--genomeSAindexNbases", "6",
         "--outFileNamePrefix", str(work / "gg_")],
        check=True, capture_output=True, cwd=work,
    )  # fmt: skip

    truth: dict[tuple[str, str], int] = {}
    injected_starts: dict[str, list[int]] = {}
    for cell in cells:
        r1, _mate, starts = _e2e_cell(contig)
        write_fastq_gz(work / units[cell, roles["umi_cdna"]], r1, prefix=f"{cell}:read")
        injected_starts[cell] = starts
        for gene, count in _E2E_TRUTH_PER_CELL.items():
            truth[cell, gene] = count

    # Asserted before the run rather than diagnosed after it: `SAM PE` over these records is exit
    # 104 out of a container, which is a legible failure only if something says what it should have
    # been. The value is derived per dataset (ADR-0035), so this is where the derivation is paid.
    assert all("--readFilesType SAM SE" in rendered["star_umi_map"][cell] for cell in cells)

    try:
        _e2e_shell(rendered["load_genome"][""], work)
        for cell in cells:
            extraction = rendered["umi_extract"][cell]
            assert re.search(r"--r1 (\S+)", extraction).group(1) == units[cell, roles["umi_cdna"]]  # type: ignore[union-attr]
            assert "--r2" not in extraction, f"the mate-less rule renders a mate:\n{extraction}"
            _e2e_shell(extraction, work)
            # The uBAM is where the derived geometry becomes bases, and here it is also where the
            # SHAPE is stated: one unpaired record per fragment, carrying the UMI injected into it
            # and exactly the 50 genomic bases it was built from.
            ubam = work / re.search(r"--out (\S+)", extraction).group(1)  # type: ignore[union-attr]
            with pysam.AlignmentFile(str(ubam), "rb", check_sq=False) as unaligned:
                records = list(unaligned.fetch(until_eof=True))
            assert not any(r.is_paired for r in records), (
                "a record flagged PAIRED with no mate beside it reads as a truncated pair, and the "
                "aligner invocation is derived from these flags rather than told"
            )
            assert len(records) == len(injected_starts[cell]) + len(_E2E_UNTAGGED_STARTS), (
                "one record per fragment, not two: the mate is the only thing that adds records"
            )
            tagged = [record for record in records if record.has_tag("UB")]
            assert len(tagged) == len(injected_starts[cell])
            assert {str(r.query_sequence) for r in tagged} == {
                contig[start : start + _E2E_READ_LEN] for start in injected_starts[cell]
            }, "the extractor cut cDNA at an offset the protocol does not publish"
            _e2e_shell(rendered["star_umi_map"][cell], work)
    finally:
        subprocess.run(
            [star, "--genomeDir", genome_dir.group(1), "--genomeLoad", "Remove",
             "--outFileNamePrefix", str(work / "unload_")],
            capture_output=True, cwd=work,
        )  # fmt: skip

    bams = {
        cell: work
        / re.search(r"--outFileNamePrefix (\S+)", rendered["star_umi_map"][cell]).group(1)  # type: ignore[union-attr]
        / "Aligned.sortedByCoord.out.bam"
        for cell in cells
    }
    # The aligner's own answer, before the counter has read anything. Two claims, and the second is
    # this run's: `NH` is the only surviving evidence that a read had somewhere else to go, and the
    # alignments come back UNPAIRED — which is what makes each of them its own fragment downstream
    # instead of half of one the counter would then go looking for.
    multimapped: dict[str, int] = {}
    for cell, bam in bams.items():
        with pysam.AlignmentFile(str(bam), "rb") as aligned:
            alignments = list(aligned.fetch(until_eof=True))
        assert not any(r.is_paired for r in alignments), (
            f"{cell} came back from a `SAM SE` invocation carrying paired records"
        )
        multimapped[cell] = len({str(r.query_name) for r in alignments if int(r.get_tag("NH")) > 1})
    assert all(n == len(_E2E_MULTIMAPPER_STARTS) for n in multimapped.values()), multimapped
    assert sum(multimapped.values()) > 0, (
        "nothing in this plate aligned to two loci, so the multimapper clause measured nothing — "
        "the duplicated block the fixture writes into the contig is what makes it a measurement"
    )

    db = _e2e_annotation(tmp_path)
    plate = [(cell, bams[cell]) for cell in cells]
    first = write_umi_counts(plate, db, tmp_path / "first.h5ad")
    second = write_umi_counts(plate, db, tmp_path / "second.h5ad")
    assert first.read_bytes() == second.read_bytes(), (
        "counting the same plate twice moved the bytes"
    )

    adata = ad.read_h5ad(first)
    matrix = count_matrix(adata).toarray()
    observed = {
        (str(cell), str(gene)): int(matrix[i, j])
        for i, cell in enumerate(adata.obs_names)
        for j, gene in enumerate(adata.var_names)
        if matrix[i, j]
    }
    assert adata.uns["primary_matrix"] == PRIMARY_MATRIX

    spurious = {key: n for key, n in observed.items() if key not in truth}
    inflated = {key: (n, truth[key]) for key, n in observed.items() if n > truth.get(key, 0)}
    recovered = sum(min(n, truth.get(key, 0)) for key, n in observed.items())
    assert spurious == {}, (
        f"counts appeared in cell/gene pairs nothing was injected into: {spurious}"
    )
    assert inflated == {}, f"counts exceeded what was injected (observed, injected): {inflated}"
    loss = 1 - recovered / sum(truth.values())
    assert loss <= 0.02, f"{loss:.1%} of injected UMIs never reached the matrix"

    # The multimapper clause, closed exactly as the paired run closes it.
    assert dict(zip(adata.obs_names, adata.obs["multimapping"], strict=True)) == {
        cell: multimapped[cell] for cell in cells
    }
    # ...and the untagged reads landed in the READ matrix instead of inflating the UMI one. Two
    # fragments, which was two reads here and four there — the number is the same because the read
    # matrices count FRAGMENTS, and that is the property the mate was never contributing to.
    reads = count_matrix(adata, "read_exon").toarray()
    gene_b = adata.var_names.get_loc("GENE_B")
    assert {int(reads[i, gene_b]) for i in range(len(cells))} == {len(_E2E_UNTAGGED_STARTS)}

    # The accounting, which only a mate-less run can state: one read in, one fragment counted.
    fragments = {cell: len(injected_starts[cell]) + len(_E2E_UNTAGGED_STARTS) for cell in cells}
    assert dict(zip(adata.obs_names, adata.obs[N_FRAGMENTS], strict=True)) == fragments
    for i, cell in enumerate(adata.obs_names):
        # Everything that went in, and the four places it may be: a distinct UMI, an internal read,
        # a multimapper, and the ONE fragment at `_E2E_REPEATED_UMI_START` whose UMI its gene
        # already carries, which is the only record allowed to disappear.
        collapsed = 1
        accounted = int(matrix[i].sum()) + int(reads[i].sum()) + multimapped[str(cell)] + collapsed
        assert accounted == fragments[str(cell)], (
            f"{cell}: {accounted} of {fragments[str(cell)]} reads reached a matrix, a fate or the "
            f"UMI they repeat; the rest left no trace, which is what a halved plate looks like"
        )


def test_two_processing_manifests_do_not_overwrite_each_other(
    built_v3: Built, tmp_path: Path
) -> None:
    """The headline use case, and the disk-state bug that would have broken it.

    compose wrote to a FIXED `.seqforge/pipeline/config.yaml`, so composing one dataset two ways left
    one config on disk — the second silently clobbering the first, along with its units.tsv and its
    materialized onlists. Keying by run_id is what makes "the same dataset paired with multiple
    processing manifests" mean anything.
    """
    manifest, reg = built_v3
    a = compose(
        manifest, _processing(manifest, processing_id="yeast"), registry=reg, workspace=tmp_path
    )
    b = compose(
        manifest,
        _processing(manifest, assembly="ce11", annotation="WS298", processing_id="worm"),
        registry=reg,
        workspace=tmp_path,
    )
    assert a.config_path != b.config_path, "two runs must not share one config path"
    assert (tmp_path / a.config_path).is_file() and (tmp_path / b.config_path).is_file()
    assert yaml.safe_load((tmp_path / a.config_path).read_text())["genome"]["assembly"] == "sacCer3"
    assert yaml.safe_load((tmp_path / b.config_path).read_text())["genome"]["assembly"] == "ce11"


def test_compose_writes_the_bound_processing_lock(built_v3: Built, tmp_path: Path) -> None:
    """Disk is STATE, not INPUT.

    compose takes no --processing on the default path — 10^4 boilerplate files nobody reads is not a
    design — but whatever decided the run must still be recoverable from disk afterwards. So the
    fully-resolved, dataset-BOUND manifest lands beside the config it produced, even when the input
    was a template with no pin.
    """
    manifest, reg = built_v3
    template = _processing(manifest, pin=False)
    assert template.dataset is None
    result = compose(manifest, template, registry=reg, workspace=tmp_path)

    lock = (tmp_path / result.config_path).parent / "processing.lock.yaml"
    assert lock.is_file()
    written = ProcessingManifest.model_validate(yaml.safe_load(lock.read_text()))
    assert written.dataset is not None, "the lock must be BOUND even when the input was not"
    assert written.dataset.dataset_hash == manifest.provenance.dataset_hash


# The params gate is the semantic check a dry run cannot make. Each corruption below takes the CLEAN
# `(spec, config)` off one shared `plan(...)` and returns a poisoned pair the gate must reject; the
# six were six near-identical functions that each re-paid the same setup.
Corruption = Callable[[kb.Spec, dict[str, object]], tuple[kb.Spec, dict[str, object]]]


def _corrupt_kb_claims_a_10bp_umi(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    """A KB claiming a 10 bp UMI over a 12 bp UMI read -- the quiet corpus killer."""
    backend = spec.require_backend()
    lying = spec.model_copy(
        update={
            "backend": backend.model_copy(update={"params": {**backend.params, "soloUMIlen": 10}})
        }
    )
    return lying, config


def _corrupt_config_drops_a_chemistry_knob(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    mangled = dict(config)
    mangled["solo"] = {k: v for k, v in solo_block(config).items() if k != "soloStrand"}
    return spec, mangled


def _corrupt_read_files_in_swaps_cdna_and_barcode(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    swapped = dict(config)
    swapped["read_files_in"] = {"cdna": "R1", "barcode": "R2"}  # barcode read fed as the cDNA read
    return spec, swapped


def _corrupt_config_disagrees_with_the_manifest(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    # makes `quantification` load-bearing: a decorative field cannot be caught.
    return spec, {**config, "solo": {**solo_block(config), "soloFeatures": "GeneFull"}}


def _corrupt_kb_declares_a_count_key(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    # belt to the schema validator's braces -- catches the model_copy'd specs tests build.
    backend = spec.require_backend()
    misowned = spec.model_copy(
        update={
            "backend": backend.model_copy(
                update={"params": {**backend.params, "soloFeatures": ["Gene"]}}
            )
        }
    )
    return misowned, config


def _corrupt_emits_a_key_with_no_owner(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    # the emitted key set must be EXACTLY the two owners' union, so an orphan (a key moved out of the
    # KB, or one never owned) is not invisible -- disjointness alone is the decorative bug in reverse.
    return spec, {**config, "solo": {**solo_block(config), "outFilterMismatchNmax": "10"}}


def _corrupt_kb_pairs_a_complex_only_match_mode_with_a_simple_chemistry(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    # `EditDist_2` is a real STAR value and a legal one -- for CB_UMI_Complex. Against this 10x
    # chemistry it is a hard FATAL raised after the genome loads, so the gate is the last place it
    # can be caught without spending a compute node to find out.
    backend = spec.require_backend()
    illegal = spec.model_copy(
        update={
            "backend": backend.model_copy(
                update={"params": {**backend.params, "soloCBmatchWLtype": "EditDist_2"}}
            )
        }
    )
    return illegal, {**config, "solo": {**solo_block(config), "soloCBmatchWLtype": "EditDist_2"}}


def _corrupt_kb_names_no_barcode_match_mode(
    spec: kb.Spec, config: dict[str, object]
) -> tuple[kb.Spec, dict[str, object]]:
    # The module dereferences the key unconditionally, so silence here is a KeyError at Snakemake
    # parse time on a compute node -- and there is no safe default to fall back to, since STAR's own
    # (`1MM_multi`) is illegal for half the chemistries in the KB.
    backend = spec.require_backend()
    params = {k: v for k, v in backend.params.items() if k != "soloCBmatchWLtype"}
    silent = spec.model_copy(update={"backend": backend.model_copy(update={"params": params})})
    return silent, config


@pytest.mark.parametrize(
    "corruption, expected_problem",
    [
        (_corrupt_kb_claims_a_10bp_umi, "soloUMIlen"),
        (_corrupt_config_drops_a_chemistry_knob, "soloStrand"),
        (_corrupt_read_files_in_swaps_cdna_and_barcode, "cdna"),
        (_corrupt_config_disagrees_with_the_manifest, "does not match the processing manifest"),
        (_corrupt_kb_declares_a_count_key, "count key"),
        (_corrupt_emits_a_key_with_no_owner, "no owner declares"),
        (
            _corrupt_kb_pairs_a_complex_only_match_mode_with_a_simple_chemistry,
            "is illegal for soloType",
        ),
        (_corrupt_kb_names_no_barcode_match_mode, "no soloCBmatchWLtype"),
    ],
    ids=[
        "kb_offsets_contradict_the_observed_layout",
        "config_drops_a_chemistry_knob",
        "read_files_in_swaps_cdna_and_barcode",
        "config_disagrees_with_the_manifest",
        "kb_declares_a_count_key",
        "emitted_key_with_no_owner",
        "kb_pairs_an_illegal_barcode_match_mode_with_the_solotype",
        "kb_names_no_barcode_match_mode_at_all",
    ],
)
def test_the_params_gate_fails_on_a_corrupt_params_dict(
    built_v3: Built, corruption: Corruption, expected_problem: str
) -> None:
    """Eight corruptions, one shared plan; silently emitting any of them is how a corpus gets poisoned.

    Each id localises the failure it is meant to catch, so a single-row regression names itself. They
    began as one function apiece, each re-paying the byte-identical `built_v3` + `plan(...)` setup.

    The two `soloCBmatchWLtype` rows are the newest and the only ones whose corruption is a value STAR
    genuinely accepts — just not from *this* chemistry. That is why the check is pairwise: neither the
    soloType nor the match mode is wrong on its own.
    """
    manifest, reg = built_v3
    p = _processing(manifest)
    spec = kb.load_spec("10x-3p-gex-v3")
    config = plan(manifest, p, registry=reg).config
    spec2, config2 = corruption(spec, config)
    status, problems = params_gate(manifest, p, spec2, config2)
    assert status == "fail"
    assert any(expected_problem in problem for problem in problems), problems


def test_compose_refuses_when_the_whitelist_cannot_be_materialized(
    built_v3: Built, tmp_path: Path
) -> None:
    manifest, _ = built_v3
    empty = OnlistRegistry(
        offline=True
    )  # no onlist registered -> no --soloCBwhitelist is emittable
    with pytest.raises(ComposeError):
        compose(manifest, _processing(manifest), registry=empty, workspace=tmp_path)


def _one_spec_per_distinct_backend() -> list[str]:
    """One representative per processing-equivalence class — the biconditional, used as leverage.

    Composing "10x-3p-gex-v3.1 specifically" is not a thing the system can do: it is byte-identical
    to v3, so the resolver picks v3 and `fill` refuses the mismatch. That is the benign-twin rule
    working, not a bug.
    And it costs no coverage: backend-identical specs render an identical config **by definition**,
    which is exactly what `processing_equivalent` asserts. So collapse the class and test one.

    Derived from `canonical_backend`, never hardcoded — if a new spec is genuinely divergent it gets
    its own case automatically, which is the property the old `{module: tech}` dict destroyed.
    """
    seen: dict[str, str] = {}
    for tech in kb.runnable_spec_ids():  # abstract family nodes have no backend to compose
        seen.setdefault(canonical_backend(kb.load_spec(tech)), tech)
    return sorted(seen.values())


@pytest.mark.parametrize("tech", _one_spec_per_distinct_backend())
def test_every_chemistry_emits_its_required_keys_and_passes_the_params_gate(
    tech: str, tmp_path: Path
) -> None:
    """One representative per processing-equivalence class, composed against the module its backend
    selects — so a chemistry cannot hide behind a hardcoded ``{module: tech}`` dict key. The axis is
    derived from ``canonical_backend``, never a hand list: a genuinely divergent new spec gets its
    own case automatically. Two facts share one ``_build`` + ``plan`` — they were byte-identical setup
    paid twice per tech:

    1. **Every key the module dereferences is emitted.** ``WorkflowModule.required_config`` says
       "checked in CI"; it matters most when a key MOVES between owners — whichever side forgets it,
       the module still declares it and the failure surfaces as a KeyError on a compute node long after
       compose exited 0. This once checked ONE hardcoded chemistry per module, which made a second
       starsolo chemistry structurally unrepresentable, so SPLiT-seq was never composed here at all. A
       param key applies to THIS chemistry only if some owner declares it (KB, derived, or the
       processing manifest): STARsolo's CB geometry is start/len for a simple chemistry and a quadruple
       for a combinatorial one, so ``required_config`` is the union of what the module MAY read and this
       is where the branch is resolved. Non-param keys are unconditional.

    2. **The params gate passes.** The three-owner coverage check — every emitted param attributable to
       exactly one of KB / derived / processing — was only ever exercised against two owners until a
       combinatorial chemistry (splitseq) reached it here.
    """
    manifest, reg = _build(tmp_path, tech)  # read ids come from the spec, not from 10x's naming
    spec = kb.load_spec(tech)
    module_name = spec.require_backend().module
    processing = _processing(manifest)
    config = plan(manifest, processing, registry=reg).config
    block = param_block_key(spec)

    # `params_gate` separately proves the block is EXACTLY its owners' union, so nothing gets to be
    # quietly absent by being quietly unowned.
    owned = set(param_owners(spec, processing))
    for dotted in get_module(module_name).required_config:
        if dotted.startswith(f"{block}.") and dotted.split(".", 1)[1] not in owned:
            continue
        assert _has_dotted(config, dotted), (
            f"{tech} -> {module_name}: config is missing required key {dotted!r}. "
            f"The module dereferences it; compose did not emit it. This is a KeyError on a "
            f"compute node, long after compose exited 0."
        )

    status, problems = params_gate(manifest, processing, spec, config)
    assert status == "pass", problems


def test_a_complex_chemistry_locates_its_barcodes_by_quadruple(
    synth_splitseq: SynthDataset,
) -> None:
    """SPLiT-seq's barcodes are derived from the element model, in whitelist order.

    `starsolo.smk` dereferenced `--soloCBstart` unconditionally, which `CB_UMI_Complex` does not
    have and cannot supply: a KeyError on a compute node, long after compose exited 0. Nothing
    caught it because no test composed splitseq.

    The quadruples are COMPUTED from the element coordinates, never transcribed: a published
    SPLiT-seq quadruple is chemistry-specific (v1 puts Round1 at 86-93, Parse/v2 at 78-85), so a
    remembered one is a coin flip between two real chemistries. Order is load-bearing — STARsolo
    pairs the Nth whitelist with the Nth position.
    """
    manifest, reg = synth_splitseq.manifest, synth_splitseq.registry
    solo = plan(manifest, _processing(manifest), registry=reg).config["solo"]
    assert isinstance(solo, dict)

    assert solo["soloType"] == "CB_UMI_Complex"
    # round1, round2, round3 -> bc1 @ [86,94), bc2 @ [48,56), bc3 @ [10,18); ends are inclusive
    assert solo["soloCBposition"] == "0_86_0_93 0_48_0_55 0_10_0_17"
    assert solo["soloUMIposition"] == "0_0_0_9"
    assert "soloCBstart" not in solo  # the simple-chemistry spelling must not appear
    assert len(str(solo["soloCBwhitelist"]).split()) == 3  # one whitelist per split-pool round


def test_a_complex_chemistry_is_refused_the_barcode_match_mode_only_simple_chemistries_may_use(
    synth_splitseq: SynthDataset,
) -> None:
    """The acceptance criterion of #198's B2: `1MM_multi` + `CB_UMI_Complex` is refused, by name.

    This is the failure mode that made `soloCBmatchWLtype` worth gating at all rather than merely
    documenting. `1MM_multi` is STAR's **global default**, so it is what a Complex spec gets by
    saying nothing — and STAR rejects it outright for `CB_UMI_Complex`. The whole class passes a
    10x-only suite: every wrong answer here leaves all seven Simple specs green and breaks only the
    four Complex ones, which is why this test composes SPLiT-seq specifically.

    Deliberately the mirror of the Simple-side row in the corruption table above: same key, opposite
    chemistry, opposite half of the measured legality matrix.
    """
    manifest, reg = synth_splitseq.manifest, synth_splitseq.registry
    processing = _processing(manifest)
    spec = kb.load_spec("splitseq")
    config = plan(manifest, processing, registry=reg).config
    assert solo_block(config)["soloType"] == "CB_UMI_Complex"
    assert params_gate(manifest, processing, spec, config) == ("pass", []), (
        "the clean pair must pass"
    )

    backend = spec.require_backend()
    illegal = spec.model_copy(
        update={
            "backend": backend.model_copy(
                update={"params": {**backend.params, "soloCBmatchWLtype": "1MM_multi"}}
            )
        }
    )
    poisoned = {**config, "solo": {**solo_block(config), "soloCBmatchWLtype": "1MM_multi"}}
    status, problems = params_gate(manifest, processing, illegal, poisoned)
    assert status == "fail"
    assert any("1MM_multi" in p and "CB_UMI_Complex" in p for p in problems), problems


def test_soloBarcodeReadLength_stays_optional_and_is_passed_only_when_declared() -> None:
    """The "make it actually run" flag: 10x sets it, SPLiT-seq must not be forced to.

    STARsolo FATALs by default unless the barcode read is exactly CB+UMI long, so 10x v2/v3/v3.1 --
    whose R1 is routinely sequenced to 150 nt -- declare `soloBarcodeReadLength: 0` to disable the
    check, and `starsolo.smk` now passes it through. But the module reads it with `SOLO.get(...)`, not
    a subscript, on purpose: a subscript would make `keys_read_by` mark `solo.soloBarcodeReadLength` a
    REQUIRED config key, and the composer would then owe it for EVERY starsolo chemistry -- including
    SPLiT-seq, which does not declare it and whose params gate forbids emitting a key it does not own.
    This test pins that optionality, so a refactor to `SOLO["soloBarcodeReadLength"]` goes red here
    rather than on a compute node running SPLiT-seq.
    """
    assert "solo.soloBarcodeReadLength" not in get_module("map/starsolo").required_config


def test_soloCBmatchWLtype_is_required_of_every_starsolo_chemistry() -> None:
    """The exact mirror of the test above, and the pair is the point.

    `soloBarcodeReadLength` is read with `SOLO.get(...)` because only some chemistries have one;
    `soloCBmatchWLtype` is read with a SUBSCRIPT because every one of them must. That difference is
    the whole mechanism by which a key becomes mandatory here — `required_config` is derived from the
    module source, so the subscript is what obliges all 11 specs to declare a value and obliges the
    params gate to police it.

    Asserted rather than assumed, deliberately: it is derived, so it "should be automatic", and a
    thing that should be automatic is exactly what nobody checks. If a refactor to `SOLO.get(...)`
    ever made it optional, a Complex spec that dropped the key would compose clean and then FATAL at
    STAR on the global default `1MM_multi`, which CB_UMI_Complex rejects.
    """
    assert "solo.soloCBmatchWLtype" in get_module("map/starsolo").required_config


def test_soloBarcodeReadLength_is_emitted_for_10x_and_not_for_splitseq(
    built_v3: Built, synth_splitseq: SynthDataset
) -> None:
    """Whether compose EMITS the key, for a chemistry that declares it and one that does not.

    Whether it then reaches the rendered STAR command is the plan text's business, and that costs a
    subprocess, so it is asserted where a dry run is already being paid for: the 10x half in
    `test_the_composed_pipeline_plans_the_h5ad_the_whitelist_and_the_command_star_receives`, the
    absent half in `test_compose_bd_enhanced_derives_the_adapter_anchored_starsolo_recipe` (which
    asserts its own `solo` keys are a superset of SPLiT-seq's, so standing in for it is checked
    rather than assumed). This half needs no subprocess at all.
    """
    v3, v3_reg = built_v3
    v3_config = plan(v3, _processing(v3), registry=v3_reg).config["solo"]
    assert isinstance(v3_config, dict)
    assert v3_config["soloBarcodeReadLength"] == "0"  # 10x declares it, so compose emits it

    ss, ss_reg = synth_splitseq.manifest, synth_splitseq.registry
    ss_config = plan(ss, _processing(ss), registry=ss_reg).config["solo"]
    assert isinstance(ss_config, dict)
    assert "soloBarcodeReadLength" not in ss_config  # SPLiT-seq does not, so it is not emitted


def test_the_required_config_scanner_can_catch_an_undeclared_key(tmp_path: Path) -> None:
    """Prove the scanner fires — a derived check that has never failed proves nothing.

    This is the load-bearing test of the pair: `required_config` is now *defined* as this scanner's
    output, so if the scanner silently missed a key, the identity test above would still pass while
    the composer dropped it. Both forms must be caught: the direct `config[...]` read and the
    `params` alias indirection that hid the real bug for as long as it existed. And prose in a
    comment must NOT be caught — the first draft of this scanner reported starsolo's own header as
    two undeclared keys.
    """
    smk = tmp_path / "fake.smk"
    smk.write_text(
        '# knobs arrive via `config["solo"]` and `config["read_files_in"]`  <- prose, not a read\n'
        'UNITS = _load_units(config["units_tsv"])\n'
        'OUT = config["outdir"]\n'
        'ASSEMBLY = config["genome"]["assembly"]\n'
        "rule r:\n"
        '    params:\n        solo=config["solo"],\n'
        '    shell:\n        r"STAR --soloCBlen {params.solo[soloCBlen]} --x {params.solo[oops]}"\n'
    )
    found = keys_read_by(smk)
    assert "solo.soloCBlen" in found  # the alias indirection resolves
    assert "solo.oops" in found  # ... and an undeclared one is visible
    assert "outdir" in found  # the direct form resolves
    assert "genome.assembly" in found  # ... including the nested form
    assert "units_tsv" in found  # the COMPOSER emits it now; no wrapper injects it
    assert "solo" not in found  # the alias BINDING is not a read of the whole block
    assert "read_files_in" not in found  # and neither is a mention in a comment


def test_the_required_config_check_can_catch_a_missing_key(built_v3: Built) -> None:
    """Prove the guard fires — a contract test that has never failed proves nothing."""
    manifest, reg = built_v3
    config = plan(manifest, _processing(manifest), registry=reg).config
    solo = solo_block(
        config
    )  # the same dict `config` holds, so deleting from it corrupts the config
    assert "soloFeatures" in solo
    del solo["soloFeatures"]
    missing = [d for d in get_module("map/starsolo").required_config if not _has_dotted(config, d)]
    assert "solo.soloFeatures" in missing
    # The position quadruples are also absent, and legitimately so: this is a CB_UMI_Simple
    # chemistry, which locates its barcode by start/length and has no quadruple to give. That is
    # why the real check intersects with `param_owners` rather than demanding the whole union.
    assert set(missing) == {"solo.soloFeatures", "solo.soloCBposition", "solo.soloUMIposition"}


def _has_dotted(config: object, dotted: str) -> bool:
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def test_params_gate_names_the_right_block_for_a_bulk_manifest(synth_bulk_pe: SynthDataset) -> None:
    """A stray ``solo`` block on a bulk config must not be misdiagnosed as "KB param dropped".

    The gate used to take "whichever of solo/bulk is a dict", so this config reported
    ``config drops KB param 'quantMode'`` — a true failure pinned on the wrong cause, which sends the
    reader to the KB when the bug is in the composer. The block is a function of the module; it is
    now read from the one definition the composer also writes through.
    """
    manifest, reg = synth_bulk_pe.manifest, synth_bulk_pe.registry
    spec = kb.load_spec("bulk-rnaseq")
    p = plan(manifest, _processing(manifest), registry=reg)
    assert params_gate(manifest, _processing(manifest), spec, p.config) == ("pass", [])

    corrupted = {**p.config, "solo": {"soloType": "CB_UMI_Simple"}}
    del corrupted["bulk"]
    status, problems = params_gate(manifest, _processing(manifest), spec, corrupted)
    assert status == "fail"
    assert any("no 'bulk' param block" in p for p in problems), problems
    assert not any("quantMode" in p and "drops" in p for p in problems), problems


def test_param_owners_computes_the_line(built_v3: Built) -> None:
    """The parse/count line as a COMPUTED FACT, directly testable, not a comment nobody re-reads."""
    from seqforge.compose import param_owners

    manifest, _ = built_v3
    owners = param_owners(kb.load_spec("10x-3p-gex-v3"), _processing(manifest))
    assert owners["soloType"] == "kb"
    assert owners["soloCBwhitelist"] == "kb"
    assert owners["soloStrand"] == "kb"
    assert owners["soloFeatures"] == "processing"  # the whole point of the move


def test_the_whitelist_is_a_rule_output_not_a_compile_time_write(
    built_v3: Built, tmp_path: Path
) -> None:
    """111 MB of barcodes is a build artifact, and compose used to write it into every run dir.

    10x's v3 whitelist is 6 794 880 barcodes. It ships packed (522 kB of deltas) and expands to
    111 MB of text that STAR opens exactly once. Compose wrote that expansion into the run directory
    at compile time, permanently, per recipe -- so one dataset compiled three ways cost a third of a
    gigabyte of identical bytes nothing ever cleaned up.

    `temp()` was also meaningless before this rule existed: the whitelist was bound to
    `starsolo_count.input` with no producing rule, and snakemake cannot delete a file it did not make.
    """
    manifest, reg = built_v3
    processing = _processing(manifest)
    result = compose(manifest, processing, registry=reg, workspace=tmp_path)
    pipeline_dir = (tmp_path / result.config_path).parent
    assert not (pipeline_dir / "onlists").exists(), "compose wrote the whitelist"

    module = (_src_root() / "workflows" / "map" / "starsolo.smk").read_text()
    assert 'temp("onlists/{name}.txt")' in module
    assert "seqforge io onlist write" in module


# ---- the live KB's read floor drops a starved cell, and the manifest keeps every one (#292) ----
#
# The floor is declared by the spec and applied here, so every test below compiles ONE plate manifest
# under a spec carrying `min_input_reads`. Where the claim is that the floor did it, the same plate is
# compiled a second time under a spec declaring none — the control, and the state the sixteen
# non-plate entries are in.
#
# The plate is built on `built_plate`, the shipped `smartseq3` entry, and that is what makes the word
# "cell" below a fact rather than a decoration: one Sample of that chemistry IS one cell, and no other
# entry may say so. A floor on a chemistry that says nothing of the kind is legal and drops *samples*
# — the second-to-last test in this section is that case, so the two nouns are pinned apart rather
# than assumed.

#: The floor these tests declare — `smartseq3`'s real number, so the arithmetic below is the shipped one.
_FLOOR = 1000


def _compose_plate(plate: DatasetManifest, reg: OnlistRegistry, workspace: Path) -> ComposeResult:
    return compose(plate, _processing(plate), registry=reg, workspace=workspace)


def test_a_cell_below_the_live_kbs_floor_leaves_the_pipeline_and_stays_in_the_manifest(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitted sample list is the POST-DROP list, and the dataset still carries every cell.

    Both halves in one test because they are one claim: `config["samples"]` is what this pipeline was
    contracted to produce, and a starved cell was never contracted — it was excluded before the
    contract was written. Dropped from the manifest instead, the same data would acquire a different
    identity every time somebody moved the threshold.
    """
    manifest, reg = built_plate
    plate = plate_of(manifest, one_run_each({"cell1": 4000, "cell2": 400, "cell3": 4000}))

    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], _FLOOR)
    result = _compose_plate(plate, reg, tmp_path)

    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["samples"] == ["cell1", "cell3"]
    units = (tmp_path / result.units_path).read_text()
    assert "cell2" not in units and "cell1" in units and "cell3" in units
    assert [s.sample_id for s in plate.experiment.samples] == ["cell1", "cell2", "cell3"]


def test_a_cells_depth_is_the_minimum_within_a_run_and_the_sum_across_them(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two runs of one cell are one cell at their combined depth; two mates of one run are not.

    `topped_up` is 600 + 600 and clears a floor of 1000 — gating its runs separately would drop both
    halves of a cell that is comfortably deep enough whole. `unequal` is a single run whose files read
    700 and 900: summing them would report a 700-read cell as 1600 and admit it, so the minimum is
    what the shallowest file can support, which is what the aligner will actually see.
    """
    manifest, reg = built_plate
    plate = plate_of(
        manifest,
        {"topped_up": {"ra": (600, 600), "rb": (600, 600)}, "unequal": {"ra": (700, 900)}},
    )

    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], _FLOOR)
    result = _compose_plate(plate, reg, tmp_path)

    assert result.admission is not None
    assert result.admission.excluded == {"unequal": 700}
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["samples"] == ["topped_up"]


def test_the_exclusion_record_carries_each_dropped_cell_its_count_the_threshold_and_the_totals(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody spots a split cell by reading 240 rows; everybody spots 768 wells on a 384-well plate.

    So the totals line is the one that does the work, and the per-cell rows are what makes the loss
    attributable. The record lives in the pipeline directory because that is the deliverable a human
    opens to answer "where did those cells go?".
    """
    manifest, reg = built_plate
    plate = plate_of(
        manifest, one_run_each({"cell1": 4000, "cell2": 400, "cell3": 4000, "cell4": 12})
    )

    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], _FLOOR)
    result = _compose_plate(plate, reg, tmp_path)

    assert result.admission is not None and result.admission.record_path is not None
    record_path = tmp_path / result.admission.record_path
    assert record_path == (tmp_path / result.config_path).parent / EXCLUSIONS_NAME
    record = record_path.read_text()
    assert "2 of 4 cells dropped" in record
    for token in ("cell2", "400", "cell4", "12", str(_FLOOR)):
        assert token in record, token
    assert "cell1" not in record and "cell3" not in record


def test_the_record_less_caveat_appears_only_when_a_drop_met_a_dataset_with_no_accession(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disclosed exactly once, where the loss is — and never as noise on a compile that lost nothing.

    Nothing in the bytes or the names says two runs are one cell, so with no accession to join on a
    cell sequenced twice arrives as two half-depth samples and a floor gates it twice. That is
    unfixable by construction, so it is disclosed rather than hidden — and only where it can have
    bitten, which is a compile that actually dropped something.
    """
    manifest, reg = built_plate
    starved = one_run_each({"cell1": 4000, "cell2": 400})
    declare_read_floor(monkeypatch, manifest.library.chemistry.value[0], _FLOOR)

    record_less = _compose_plate(plate_of(manifest, starved), reg, tmp_path / "no-accession")
    assert record_less.admission is not None and record_less.admission.record_path is not None
    disclosed = (tmp_path / "no-accession" / record_less.admission.record_path).read_text()
    assert "filename" in disclosed

    archived = _compose_plate(
        plate_of(manifest, starved, accession="SRR1000001"), reg, tmp_path / "accession"
    )
    assert archived.admission is not None and archived.admission.record_path is not None
    joined = (tmp_path / "accession" / archived.admission.record_path).read_text()
    assert "filename" not in joined

    healthy = _compose_plate(
        plate_of(manifest, one_run_each({"cell1": 4000})), reg, tmp_path / "healthy"
    )
    assert healthy.admission is not None and healthy.admission.excluded == {}
    assert healthy.admission.record_path is None, (
        "nothing was lost, so there is nothing to disclose"
    )


def test_compose_refuses_a_dataset_whose_every_cell_is_below_the_floor(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `rule all` at exit 0 is the silent-success failure class, so this is a refusal.

    There is deliberately no drop-rate gate above it: a plate with 60% dud wells is real, and a rate
    threshold needs a number nobody can defend. Nothing left to produce is the one defensible line.
    """
    manifest, reg = built_plate
    plate = plate_of(manifest, one_run_each({"cell1": 40, "cell2": 400}))

    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], _FLOOR)
    with pytest.raises(ComposeError, match="2 of 2"):
        _compose_plate(plate, reg, tmp_path)


def test_compose_refuses_a_manifest_that_measured_no_reads_rather_than_gating_it_as_empty(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Not measured" and "zero reads" must not be the same value to a gate.

    A manifest written before per-file counts existed measured nothing, and reading that as zero would
    drop every cell in it — silently, and at the one moment the compiler looks most confident. Refuse
    and name the fix instead.
    """
    manifest, reg = built_plate
    plate = plate_of(manifest, one_run_each({"cell1": 4000, "cell2": 4000}))
    unmeasured = plate.model_copy(
        update={"provenance": plate.provenance.model_copy(update={"estimated_reads": {}})}
    )

    declare_read_floor(monkeypatch, unmeasured.library.chemistry.value[0], _FLOOR)
    with pytest.raises(ComposeError, match="read count"):
        _compose_plate(unmeasured, reg, tmp_path)


def test_the_drop_is_invisible_to_the_dataset_hash(
    built_plate: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two thresholds, two sample lists, one unmoved identity — the reason no verdict is stored.

    Freeze an exclusion list into the write-once manifest and raising the floor from 1000 to 1500
    gives the SAME DATA A DIFFERENT IDENTITY, which is exactly what a content hash invariant under
    processing change exists to prevent. So compose applies the floor and records the outcome in its
    own output; the manifest is read and never rewritten.
    """
    manifest, reg = built_plate
    plate = plate_of(manifest, one_run_each({"cell1": 4000, "cell2": 400, "cell3": 4000}))
    before = dataset_content_hash(plate)

    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], None)
    ungated = _compose_plate(plate, reg, tmp_path / "no-floor")
    declare_read_floor(monkeypatch, plate.library.chemistry.value[0], _FLOOR)
    gated = _compose_plate(plate, reg, tmp_path / "floor")

    ungated_config = yaml.safe_load((tmp_path / "no-floor" / ungated.config_path).read_text())
    gated_config = yaml.safe_load((tmp_path / "floor" / gated.config_path).read_text())
    assert ungated_config["samples"] == ["cell1", "cell2", "cell3"]
    assert gated_config["samples"] == ["cell1", "cell3"]
    assert ungated.admission is None, "a spec declaring no floor runs no gate at all"
    assert dataset_content_hash(plate) == before == plate.provenance.dataset_hash


def test_a_floor_on_a_chemistry_whose_sample_is_not_a_cell_drops_samples_and_says_so(
    built_v3: Built, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A floor is an admission threshold ANY chemistry may declare; only a plate makes it a *cell*.

    The two declarations are independent, and this is the half nothing ships: `smartseq3` declares
    both, so every other test in this section reads the word "cells" and would keep reading it if the
    noun were hard-coded. Here a 10x library — demultiplexed in the read, so one Sample is one library
    and never one cell — carries a floor, and what leaves the pipeline is a *sample*.

    It also pins the shape of the fixture that hands the composer a spec. Declaring a floor must not
    require declaring the cell axis, because the knowledge base refuses that flag beside a per-sample
    module: a fixture that set it anyway would be proving this gate against a spec `load_spec` cannot
    produce, and no amount of green here would mean anything.
    """
    manifest, reg = built_v3
    libraries = plate_of(manifest, one_run_each({"lib1": 4000, "lib2": 400}))
    assert not kb.load_spec(libraries.library.chemistry.value[0]).identity.sample_is_cell

    declare_read_floor(monkeypatch, libraries.library.chemistry.value[0], _FLOOR)
    result = _compose_plate(libraries, reg, tmp_path)

    assert result.admission is not None and result.admission.record_path is not None
    assert result.admission.summary == "1 of 2 samples dropped"
    record = (tmp_path / result.admission.record_path).read_text()
    assert "1 of 2 samples dropped" in record and "cell" not in record
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["samples"] == ["lib1"]


def test_a_chemistry_that_declares_no_floor_makes_the_composer_add_no_step(
    built_v3: Built, tmp_path: Path
) -> None:
    """The whole path stays inert on the sixteen entries that declare none: no gate, no record, no key.

    `min_input_reads` defaults to `None`, and only the plate chemistry departs from the default — so
    every dataset seqforge compiles under any of the others takes the byte-for-byte path it took
    before this existed, which is what makes the gate cheap to carry rather than a step every compile
    pays for. The one entry that DOES declare a floor is exercised two tests up, against a plate.
    """
    manifest, reg = built_v3
    declaring = {
        sid for sid, spec in kb.load_all_specs().items() if spec.min_input_reads is not None
    }
    assert declaring == {"smartseq3"}
    assert kb.load_spec(manifest.library.chemistry.value[0]).min_input_reads is None

    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)

    assert result.admission is None
    assert not ((tmp_path / result.config_path).parent / EXCLUSIONS_NAME).exists()
