"""Tests for ``seqforge.compose`` — plan, the emitted config/units.tsv, and the compose gates.

The composer turns ``(dataset, processing)`` into a Snakefile + config.yaml + units.tsv. The params
gate is the semantic check a dry run cannot make, so it gets adversarial coverage: a KB whose
declared offsets contradict the observed layout, and a config that drops or mangles a
chemistry-defining knob, must both FAIL — silently emitting them is how a corpus gets poisoned.

The shared build helpers (``built_v3``, ``_build``, ``_processing`` …) live in ``tests/conftest.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from conftest import (
    Built,
    DryRun,
    SynthDataset,
    _build,
    _processing,
    _rule_blocks,
    _src_root,
    solo_block,
)
from seqforge import kb
from seqforge.compose import ComposeError, compose, core, params_gate, plan
from seqforge.compose.params import param_block_key, param_owners
from seqforge.io import OnlistRegistry
from seqforge.models.processing import ProcessingManifest
from seqforge.resolve.confuse import canonical_backend
from seqforge.workflows import get_module, keys_read_by, list_modules
from seqforge.workflows.memory import bam_sort_ram


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
    assert units[0].split("\t") == ["sample_id", "run", "read_id", "path"]
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
    """units.tsv carries a ``run`` column, from the same `run_key` that grouped the dataset.

    Recording the run is what lets the mapping module pair a pooled sample's mates without re-parsing
    filenames. The value must be `resolve.group.run_key`, not a second notion of "run" -- one function
    owns it.
    """
    from seqforge.compose.core import _units
    from seqforge.resolve.group import run_key

    manifest, reg = built_v3
    rows = _units(manifest)
    assert rows and all(set(r) >= {"sample_id", "run", "read_id", "path"} for r in rows)
    for r in rows:
        assert r["run"] == run_key(r["path"])


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
    assert header == ["sample_id", "run", "read_id", "path"]
    sid = units_path.read_text().splitlines()[1].split("\t")[0]
    # run -> {role -> filename}; filename order is the REVERSE of run order, so a filename sort would
    # mispair. read_files_in is cdna=R2, barcode=R1.
    f = {
        "r1": {"R1": "z_bc.fastq.gz", "R2": "z_cdna.fastq.gz"},
        "r2": {"R1": "a_bc.fastq.gz", "R2": "a_cdna.fastq.gz"},
    }
    # rows deliberately SCRAMBLED across mates
    rows = [
        [sid, "r2", "R2", f["r2"]["R2"]],
        [sid, "r1", "R1", f["r1"]["R1"]],
        [sid, "r2", "R1", f["r2"]["R1"]],
        [sid, "r1", "R2", f["r1"]["R2"]],
    ]
    units_path.write_text("\n".join("\t".join(r) for r in [header, *rows]) + "\n")
    for _sid, _run, _rid, path in rows:  # `snakemake -n` needs its source inputs to exist
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
    # nTrOutSAM)` writes exactly the top-scoring alignment, which is the record that filter keeps, so
    # the retained CRAM is unchanged and both the sort budget and the wall-clock are cheaper. Like the
    # CellRanger set above it is a module literal, and argv is the only place a module literal is
    # visible at all.
    assert "--outSAMmultNmax 1" in planned

    # The sort budget, to the byte, because the wiring is what only a real `snakemake -n` can prove:
    # `sort_ram` is a params callable over `resources`, evaluated after snakemake has resolved this
    # attempt's `mem_mb`, so a construct snakemake declines to hand `resources` fails the plan rather
    # than rendering. The exact value is what says the number STAR receives came through that path and
    # is 3/4 of what the job was granted; the loose `\d+` it replaces matched a fall-back constant,
    # a mis-scaled MiB figure and STAR's own default equally well.
    mem_mb = yaml.safe_load((tmp_path / result.config_path).read_text())["mem_mb"]
    assert isinstance(mem_mb, int)
    assert f"--limitBAMsortRAM {bam_sort_ram(mem_mb)}" in planned, (
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
    manifest, reg = synth_bulk_pe.manifest, synth_bulk_pe.registry
    result = compose(manifest, _processing(manifest), registry=reg, workspace=tmp_path)
    assert result.modules[0].name == "map/star"
    assert result.gate["params"] == "pass"
    config = yaml.safe_load((tmp_path / result.config_path).read_text())
    assert config["bulk"]["quantMode"] == "GeneCounts"
    assert config["read_files_in"] == {"mate1": "R1", "mate2": "R2"}
    assert "solo" not in config


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
    spec = kb.load_spec("bulk-rnaseq-pe")
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
