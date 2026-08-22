# workflows/map/starsolo.smk  --  HAND-WRITTEN, VERSIONED, CI-TESTED. NEVER machine-generated.
#
# STARsolo mapping for barcoded single-cell RNA-seq (10x 3' v2/v3, SPLiT-seq, ...). The composer
# emits `config.yaml` + `units.tsv` and selects this module by id `map/starsolo`; it NEVER writes
# rule source. Every chemistry-defining knob arrives via `config["solo"]` (rendered from the KB's
# backend.params and asserted by compose's params gate); the read->role placement arrives via
# `config["read_files_in"]`, cDNA FIRST.
#
# The genome index resolves at RUN TIME from a `liulab-genome` assembly id — no genome path
# is ever baked into a config or a manifest, and we do not reimplement liulab-genome's job here.

import csv

# seqforge's own helpers, imported rather than restated. `h5ad_suffixes` decides both what the
# packaging rule DECLARES below and what `seqforge io h5ad` WRITES, so the two cannot drift; a rule
# that declared its outputs separately from the code producing them would be two sources of truth for
# one fact, which is the bug this repo keeps finding. `memory` is the same move applied to the
# arithmetic that sizes this module's one expensive rule: a Snakefile is not importable, so a
# constant and a closure living here could never be unit-tested, only run. `QC_SUFFIX` is that same
# rule applied to the last name this file used to spell for itself, and it is the one where drift
# would be SILENT: `workflows/qc.py` writes that bundle and now reads it back for the report, so a
# rename here would leave the rule producing a file the reader stops finding -- and a report that
# finds nothing looks exactly like a pipeline that has not run. All three imports make the same
# assumption `rule genome_index` already makes of `genome`: the env running snakemake is the env that
# has them.
from seqforge.workflows.h5ad import (
    STAR_BAM,
    STAR_LOG_FILES,
    h5ad_suffixes,
    solo_filtered_files,
    solo_raw_files,
    solo_stats_files,
)
from seqforge.workflows.memory import (
    STARSOLO_RETRIES,
    bam_sort_ram,
    escalated_mem_mb,
    index_mem_mb,
)
from seqforge.workflows.qc import QC_SUFFIX
from seqforge.workflows.starsolo_args import SORT_CAP_SHELL, starsolo_shell_args
from seqforge.workflows.threads import QC_BUNDLE_THREADS
from seqforge.workflows.units import ordered_fastqs


def _load_units(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


UNITS = _load_units(config["units_tsv"])
SAMPLES = sorted({u["sample_id"] for u in UNITS})
OUTDIR = config["outdir"]
GENOME = config["genome"]
ASSEMBLY = GENOME["assembly"]
# The longest gap STAR may open on this assembly, recorded in the recipe by the processing policy off
# liulab-genome's shipped table. `.get`, not a subscript, and that is the whole mechanism by which an
# assembly the lab has not characterised emits no flag instead of refusing to compose: the scanner
# that derives `required_config` counts a subscript and not a `.get`, so a subscript here would make
# the composer owe this key for every dataset in the corpus.
INTRON_MAX = GENOME.get("intron_length_cap")
SOLO = config["solo"]
# STAR takes --soloFeatures as N space-separated values and writes one Solo.out/<Feature>/ per value.
FEATURES = SOLO["soloFeatures"].split()
PRIMARY = config["primary_feature"]

# The shared genome segment is loaded once and attached by every mapping job, so the rule that loads
# it needs a file to hang a dependency on. A flag rather than a directory: nothing reads its bytes,
# and what a mapping job actually depends on is that the load HAPPENED. It sits BESIDE the resolved
# index rather than inside it -- a file under another rule's directory output is a child of that
# output, which snakemake refuses to build a DAG for at all.
LOADED_FLAG = f"{OUTDIR}/index/{ASSEMBLY}.loaded"


def fastqs(sample, role):
    # `ordered_fastqs` owns the order and the argument for it; the two other mapping modules read the
    # same one. Mismatched RUNS do FATAL here ("quality string length is not equal to sequence
    # length") -- mismatched LANES are the silent half, and are why the order is not `path` alone.
    return ordered_fastqs(UNITS, sample, role)


def readfilesin(sample, *roles):
    """Render STAR ``--readFilesIn`` for one sample: each role (a mate) is its FASTQs **comma-joined**,
    and the mates are space-separated -- ``cdna1,cdna2 barcode1,barcode2``.

    A sample pooled across N sequencing runs (or across one run's lanes) passes every such file for a
    mate as one comma-list, in the single order ``fastqs`` imposes on every mate alike. This is STAR's
    own multi-file syntax; joining with spaces instead -- the old bug -- makes STAR read the extra
    files as extra mates and crash. A single-run sample renders one file per mate, exactly as before,
    so this generalises to any run and lane count with no special case."""
    return " ".join(",".join(fastqs(sample, role)) for role in roles)


def whitelists():
    """One path for 10x; three for a split-pool chemistry. The config value is the argv rendering."""
    return SOLO["soloCBwhitelist"].split()


# STAR's command line -- geometry, clips, the anchored-bead adapter, the CellRanger-parity set and
# the SAM write path -- is rendered by `workflows/starsolo_args.py` and reaches the shell block below
# as ONE token. Five closures used to live here, reading a module-level `SOLO`, and they were the
# reason `e2e.run_starsolo` could not run a `CB_UMI_Complex` chemistry: it named the four simple
# geometry flags by hand because the branch that chooses them was in a file nothing can import. The
# instrument now calls the same renderer, and it omitted NINE shipped flags until it did (#348).
#
# It is the move this file's header already argues for `h5ad`, `memory`, `QC_SUFFIX` and
# `ordered_fastqs`: a Snakefile is not importable, so a closure written inside one can never be
# unit-tested, only run. Every one of those five now has a test.
#
# `required_config` follows the reads rather than the file: `workflows/__init__.py::argv_keys_read_by`
# walks the renderer's AST for the `solo[...]` subscripts that used to be scanned out of this source,
# which sees BOTH geometry branches where a scan could only ever see the one it took.


# Every raw matrix/axis file this run's --soloFeatures must produce, per sample -- declared
# file-by-file, and that is the point. `starsolo_count` used to declare
# `directory(f"{OUTDIR}/{{sample}}/Solo.out")`, under which STAR writing three of five features and
# exiting 0 was indistinguishable from success: the directory exists, snakemake is satisfied, and the
# missing counts surface later as an h5ad nobody can explain. A named output cannot be missing.
# The `{{{{sample}}}}` is snakemake's usual escape -- expand() fills `f` and leaves `sample` a wildcard.
SOLO_MATRICES = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_raw_files(FEATURES))

# The rest of what STAR writes, declared so the finalize rules can consume it and `temp()` can then
# delete it -- automatic, DAG-ordered cleanup, never a manual `rm`. Same file-by-file discipline as
# SOLO_MATRICES: a declared output STAR did not write fails the rule loudly. The stats + logs + the
# filtered/ tree feed `qc_bundle`; the BAM feeds `solo_to_cram`.
SOLO_STATS = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_stats_files(FEATURES))
SOLO_FILTERED = expand(f"{OUTDIR}/{{{{sample}}}}/Solo.out/{{f}}", f=solo_filtered_files(FEATURES))
STAR_LOGS = expand(f"{OUTDIR}/{{{{sample}}}}/{{f}}", f=list(STAR_LOG_FILES))


rule all:
    input:
        # ONE FILE PER SAMPLE, and it is the QC bundle. The count objects and the CRAM are `input:`
        # of the rule that writes it, so demanding the bundle demands the whole sample and a target
        # list naming it cannot be quietly incomplete. This REPLACES an enumeration: all three used
        # to be named here precisely because nothing downstream consumed the first two, and a
        # deliverable nobody demands simply stops being produced -- which made every new deliverable
        # a name somebody had to remember to add. A consumer holds that property structurally, so
        # there is nothing left here to forget.
        expand(f"{OUTDIR}/{{sample}}/{{sample}}{QC_SUFFIX}", sample=SAMPLES),


rule onlist:
    """Materialize one barcode whitelist, for STAR to read once and snakemake to then delete.

    `temp()` is the entire point, and why -- the 111 MB a compiled run used to carry three times over,
    and why an input with no producing rule was `temp()`-able in name only -- is argued once in
    ADR-0015.

    No `container:` directive, deliberately. This runs `seqforge`, which is not an aligner -- the
    ambient environment is the one that just ran `seqforge compose`, so it is by construction the one
    that has it. Naming `align-rna` here would put our own tool inside STAR's image.
    """
    output:
        temp("onlists/{name}.txt"),
    localrule: True
    shell:
        "seqforge io onlist write {wildcards.name} --out {output}"


rule genome_index:
    """Resolve the STAR index via liulab-genome at run time (never a path in the manifest).

    This rule only **looks up** the index; it never builds one. `get_star_index` returns the genomeDir
    liulab-genome already built for this assembly + annotation, and **raises if none exists** -- the
    index is liulab-genome's artifact, built ahead of the run by its own machinery, in its own
    environment. A machine with no prebuilt index fails loudly here ("build it first"), which is the
    failure mode we want: the pipeline consumes the index, it does not decide when or how it is built.

    Because nothing is invoked here -- no STAR, no `genomeGenerate` -- this rule needs no tool on PATH
    and no `container:`. (A `container:` would be moot anyway: snakemake wraps a container around a
    `shell:` command in `shell.py`, but a `run:` block executes Python in the snakemake process and
    never passes through that wrap; snakemake's own linter excludes `is_run` rules from "missing
    software definition".) The container on the alignment rule pins the aligner that does the work.
    """
    output:
        directory(f"{OUTDIR}/index/{ASSEMBLY}"),
    params:
        assembly=ASSEMBLY,
        annotation=GENOME["annotation"],
    run:
        from pathlib import Path

        from genome import Genome

        index = Genome(params.assembly).get_star_index(gtf=params.annotation)
        out = Path(output[0])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.symlink_to(index)


rule load_genome:
    """Load the genome index into SHARED memory once, and hand every mapping job something to attach to.

    **This rule exists because of concurrency arithmetic, not as a tuning preference.** STAR's index
    is per-process and resident for the life of the job, so N samples mapping at once on one machine
    cost N copies of it: six droplet samples against a ~31 GB human index is ~186 GB of index where
    ~31 GB would do. A composed pipeline runs on ONE machine (ADR-0051), which is what makes one
    segment attachable by every job at all -- and it is also what makes the multiplication real,
    since those jobs are concurrent by construction rather than spread out by a scheduler.

    `map/star-umi` reached this same rule from the other direction, and the two arguments are worth
    keeping apart: a plate re-LOADS the index once per cell, which is I/O, while a droplet run holds
    several copies at once, which is footprint. Either one on its own is a reason to share.

    **`Remove` FIRST, defensively, and it is safe.** `shmctl(IPC_RMID)` *marks* a segment for
    destruction: a process already attached keeps running, and the memory goes when the last one
    detaches. It cannot yank memory out from under a concurrent job on the same index. That is worth
    saying because the line reads dangerous and is not -- and because a stale segment left by a
    killed run is otherwise inherited silently. `|| true` because removing a segment that is not
    there is a STAR error and a no-op, and this rule must not fail for having nothing to clean.

    The same idiom `starsolo_count` already opens with (`rm -rf ..._STARtmp`), one level up: clear
    the stale thing you cannot otherwise reach, then do the work.

    **Neither invocation writes into the pipeline directory.** STAR drops a log, a progress log and a
    `_STARtmp/` under whatever prefix it is handed and removes none of them, so these two used to
    leave nine undeclared entries beside the index and the flag, with nothing saying which two of
    the eleven were output. The prefix is therefore a directory this block MAKES and destroys.
    Pointing it somewhere else inside the run directory, or sweeping afterwards by glob, both work
    only for as long as they stay configured correctly; a scratch that cannot outlive the shell has
    nothing to configure and nothing to sweep. The `trap` is what covers the failing path, which is
    the path that leaves the most behind.

    **Nothing verifies separately that the sharing happened, and that is deliberate.** If the index
    cannot be loaded STAR exits non-zero, this rule fails and snakemake stops -- which covers a bad
    index path and a kernel refusing the segment. A container namespacing IPC (apptainer does not by
    default, but `--ipc` would) makes every job load privately with no error anywhere; the cost there
    is speed rather than correctness, and the setting belongs to whoever submits the pipeline.
    """
    input:
        index=rules.genome_index.output,
    output:
        # Not `temp()`: deleting the flag would tell snakemake the load never happened, and a rerun
        # would reload a segment that is already resident.
        touch(LOADED_FLAG),
    container: config["container"]
    threads: config["threads"]
    resources:
        # The one rule that holds the genome segment, so a scheduler told nothing about it packs jobs
        # beside the largest allocation on the machine without knowing it is there. The recipe's whole
        # figure, which is an upper bound on this residency -- see `index_mem_mb`.
        mem_mb=lambda wildcards, attempt: index_mem_mb(config["mem_mb"], attempt),
    shell:
        r"""
        # STAR's run-files go to a directory made here and destroyed here, so none of them can reach
        # the pipeline directory. The trap fires on the failing path too.
        scratch=$(mktemp -d)
        trap 'rm -rf "$scratch"' EXIT
        # Defensive: marks any stale segment for destruction. Attached jobs keep running; a segment
        # that is not there is a no-op we must not fail on.
        STAR --genomeDir {input.index} --genomeLoad Remove \
             --outFileNamePrefix "$scratch"/remove_ > /dev/null 2>&1 || true
        STAR --genomeDir {input.index} --genomeLoad LoadAndExit \
             --outFileNamePrefix "$scratch"/
        """


rule starsolo_count:
    """Map one sample's cDNA + barcode reads to a per-cell count matrix, and to a BARCODED alignment.

    The shell block clears STAR's `_STARtmp` before invoking STAR, so every (re)run is
    preemption-safe: a preempted STAR leaves `results/<sample>/_STARtmp` behind, STAR ABORTS a rerun
    if it already exists, and snakemake cannot remove it (an undeclared output).

    `--outSAMtype BAM SortedByCoordinate` is not a preference either (#198). STARsolo writes only the
    cDNA mate into the BAM, and the barcode lives solely in the other mate, so with no CB/UB tag the
    barcode is IRRECOVERABLY absent from the retained alignment -- which is what made 920 GiB of
    shipped CRAM unable to do any of the three things a retained alignment is for. STAR emits those
    tags in the sorted BAM and nowhere else ("CB and/or UB attributes in --outSAMattributes can only
    be output in the sorted BAM"), so the sort is the price of the barcode. It is also a refund: the
    finalize rule no longer re-sorts, and the resulting CRAM measured 12% SMALLER than the
    barcode-less one it replaces.

    **The memory request ESCALATES with the attempt, and STAR's sort cap escalates with it** (#205).
    STARsolo holds allocations `--limitBAMsortRAM` does not bound -- chiefly `readInfo`, at 16 bytes
    times every input read -- so a large sample used to be OOM-killed by the scheduler rather than
    refused by STAR, dying with a signal and no number. `retries:` plus a `mem_mb` that is a function
    of `attempt` gives such a sample two more tries at 2x and 3x, while attempt 1 stays byte-identical
    to what a normal sample was always given. A sample that exhausts the retries FAILS, loudly and
    deliberately: at that point the answer is a recipe with a bigger `resources.mem_gb`, chosen by
    someone who looked at the sample, not a third blind doubling. The arithmetic and the measurements
    behind it live in `workflows/memory.py`.

    **The index is ATTACHED, not loaded** (#379). `load_genome` put one copy in shared memory and
    this rule depends on its flag, so N concurrent samples on the machine hold one index between them
    rather than one apiece. `--genomeLoad LoadAndKeep` reaches STAR through
    `workflows/starsolo_args.py` like every other flag on this command line, and it is why
    `--limitBAMsortRAM` is required rather than merely wise: STAR's default of `0` means "reuse the
    genome allocation", and under a shared copy there is none of its own to reuse. The memory request
    does NOT shrink to match the sharing -- it covers a job alone on the machine (ADR-0051), which is
    what it would be the first time one sample ran by itself.
    """
    input:
        cdna=lambda wc: fastqs(wc.sample, config["read_files_in"]["cdna"]),
        barcode=lambda wc: fastqs(wc.sample, config["read_files_in"]["barcode"]),
        index=rules.genome_index.output,
        loaded=rules.load_genome.output,
        whitelist=whitelists(),
    output:
        # `temp()` on everything: the raw matrices are consumed by `solo_to_h5ad`, the stats +
        # filtered tree + logs by `qc_bundle`, and the BAM by `solo_to_cram`. Snakemake deletes each
        # group once its one consumer finishes -- so nothing here survives that is not a `rule all`
        # target. The files stay declared (not just `rm`'d) so a missing one is still a rule failure.
        matrices=temp(SOLO_MATRICES),
        stats=temp(SOLO_STATS),
        filtered=temp(SOLO_FILTERED),
        logs=temp(STAR_LOGS),
        bam=temp(f"{OUTDIR}/{{sample}}/{STAR_BAM}"),
    # The pinned aligner: liulab-runtime's `align-rna`, resolved by compose to a ghcr tag or to a
    # prebuilt .sif on this machine. Naming it here is CONSUMING liulab-runtime's artifact, not
    # defining an environment -- no conda YAML, no Dockerfile, and no dependency table this rule can
    # resolve against. The STAR a test execs lives in a test-only environment no rule can see.
    #
    # Honoured only when the run passes `--software-deployment-method apptainer` (measured: without
    # it, snakemake plans the same jobs and never mentions the image). That is snakemake's contract
    # and it is the user's call -- they submit, we do not.
    container: config["container"]
    threads: config["threads"]
    # `retries:` and `resources:` are ONE mechanism, which is why they are read together. Declaring
    # `mem_mb` gates the scheduler AND gives the coordinate sort a real budget instead of the
    # genome's (see `bam_sort_ram`); it moved here from `solo_to_cram`, which is where the sort used
    # to happen, so the memory is spent in the rule that does the sorting. What is new (#205) is that
    # BOTH numbers are functions of snakemake's 1-based `attempt`: attempt 1 is `config["mem_mb"]`
    # exactly, so nothing changes for a sample that fits, and a sample killed for overrunning it gets
    # 2x and then 3x rather than failing identically twice. `config["mem_mb"]` still appears here as
    # a literal subscript on purpose -- `workflows/__init__.py::keys_read_by` SCANS this source to
    # compute `required_config`, and a key the scanner cannot see is a key the composer is not
    # obliged to emit, i.e. a KeyError on a compute node long after compose exited 0.
    #
    # THE SORT CAP IS A `resources:` ENTRY, NOT A `params:` ONE, and that is not a style choice --
    # it is the only construct snakemake re-evaluates per attempt. MEASURED against the pinned
    # 9.23.1, because the plausible version of this is wrong: `Job.attempt`'s setter (`jobs.py`)
    # clears `self._resources` and NOT `self._params`, and `reset_params_and_resources()` is
    # one-shot behind `_params_and_resources_resetted`. So a `params:` callable -- even one taking
    # `resources`, which snakemake does pass -- is expanded once, on attempt 1, and every retry
    # reuses that value verbatim. A three-attempt run of exactly that shape traced
    # `mem=1000 cap=750 / mem=2000 cap=750 / mem=3000 cap=750`: the request escalates and the cap
    # does not. That is worse than no retry at all -- attempt 2 buys scheduler memory STAR is still
    # forbidden to sort in, spends a second multi-hour queue slot, and fails for the reason attempt
    # 1 already recorded. As a resource the same run traces 750 / 1500 / 2250.
    #
    # The name carries its unit (`_bytes`) because it is the one number here that is not MiB: STAR
    # takes `--limitBAMsortRAM` in bytes, and a resource is a bare integer with nowhere else to say
    # so. It is a custom resource, so nothing constrains it unless a `--resources` flag names it.
    retries: STARSOLO_RETRIES
    resources:
        mem_mb=lambda wildcards, attempt: escalated_mem_mb(config["mem_mb"], attempt),
        bam_sort_ram_bytes=lambda wildcards, attempt: bam_sort_ram(
            escalated_mem_mb(config["mem_mb"], attempt)
        ),
    params:
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/",
        # The whole command line, quoted, as ONE token. cDNA mate first, then barcode mate (order
        # asserted by the params gate); each mate is its runs and lanes comma-joined, so a pooled
        # sample maps in one STAR pass. See readfilesin().
        #
        # A `params:` callable is expanded ONCE, on attempt 1 -- the same measurement that makes the
        # sort cap a `resources:` entry. So the cap is NOT in here: it reaches the shell template as
        # `SORT_CAP_SHELL`, below, where snakemake re-expands it per attempt. Nothing else about the
        # command line varies with the attempt, so nothing else needs to escape this token.
        argv=lambda wc, input, threads: starsolo_shell_args(
            SOLO,
            genome_dir=input.index,
            cdna=readfilesin(wc.sample, config["read_files_in"]["cdna"]),
            barcode=readfilesin(wc.sample, config["read_files_in"]["barcode"]),
            whitelist=input.whitelist,
            out_prefix=f"{OUTDIR}/{wc.sample}/",
            threads=threads,
            # The assembly's junction bound, from the recipe. Stated even when it is `None`, because
            # the renderer requires it: "this assembly has no registered cap" and "this module forgot
            # to ask" must not be able to arrive at STAR as the same command line.
            intron_max=INTRON_MAX,
        ),
    shell:
        # STAR's whole command line is `workflows/starsolo_args.py`'s. Every literal that used to be
        # spelled out here -- the CellRanger-parity set, the SAM write path, the geometry branch, the
        # clips -- lives beside the reasoning that chose it and the verification against the pinned
        # 2.7.11b, and reaches this block already rendered and quoted. There is nothing left to spell,
        # which is the point: the instrument that renders STARsolo's OTHER command line omitted nine
        # of these flags, and a block with no flags in it cannot be the half that drifts (#348).
        #
        # The sort cap is the one exception and it is not a style choice: it must re-expand per retry
        # attempt, and a `params:` value is expanded once. `SORT_CAP_SHELL` spells the flag so this
        # file does not.
        "rm -rf {params.prefix}_STARtmp\n"
        "STAR {params.argv} " + SORT_CAP_SHELL


rule solo_to_h5ad:
    """Package Solo.out's raw matrices as .h5ad -- THE deliverable of this pipeline.

    A `shell:` calling a seqforge verb, not a `run:` block, and that is deliberate: `snakemake -n -p`
    renders every shell block while planning and cannot see inside a `run:` block, so this way
    compose's wiring gate covers the packaging step too. It is also the CLI-is-the-API line.

    No `container:`. Writing an .h5ad is seqforge's own output-format job, not an aligner's; `anndata`
    is a plain dependency of this package. Only `starsolo_count` needs liulab-runtime.
    """
    input:
        matrices=rules.starsolo_count.output.matrices,
    output:
        expand(f"{OUTDIR}/{{{{sample}}}}/{{{{sample}}}}{{suffix}}", suffix=h5ad_suffixes(FEATURES)),
    params:
        solo=lambda wc: f"{OUTDIR}/{wc.sample}/Solo.out",
        prefix=lambda wc: f"{OUTDIR}/{wc.sample}/{wc.sample}",
        features=" ".join(FEATURES),
        primary=PRIMARY,
    shell:
        r"""
        seqforge io h5ad --solo-dir {params.solo} --features "{params.features}" \
             --primary {params.primary} --out-prefix {params.prefix}
        """


rule solo_to_cram:
    """Compact STAR's coordinate-sorted BAM into a CRAM, then let `temp()` drop the BAM.

    A sibling of `solo_to_h5ad`: both consume `starsolo_count` and nothing else, so snakemake runs
    them in parallel. The reference is resolved at run time from the assembly id via liulab-genome
    (never a baked path); no `embed_ref`, so the CRAM carries the reference MD5 in its header and the
    assembly id is recorded in the QC bundle.

    It no longer sorts, and that deleted a defect rather than a step (#198). The `samtools sort` this
    verb used to run was built with no `-T`, so its temp files landed in the CWD as
    `samtools.<pid>.<tid>.tmp.NNNN.bam` -- undeclared, so a preempted sort leaked them and snakemake
    could not clean up what it had not made (41.4 GiB had accumulated across five pipeline dirs). The
    fix is not a `-T` pointing somewhere snakemake owns: STAR sorts now, so there is no second sort
    to configure correctly. Prefer the mechanism that cannot leak over the one that must be set up
    right. With no sort there is also nothing here to spend memory on, so the rule declares no
    `resources: mem_mb` -- that budget moved to `starsolo_count`, which is where the sorting went.

    `container:`, unlike `solo_to_h5ad`. This rule shells out to **samtools**, a runtime binary -- so,
    exactly like `starsolo_count`'s STAR, the tool must come from the pinned `align-rna` image and not
    from "whatever the submitting shell happened to have". `align-rna` carries samtools (its base
    layer), seqforge and liulab-genome (its `lab` feature), so `seqforge io cram` runs fully inside it.
    The h5ad/onlist/bundle steps stay container-less because they invoke no external binary; this one
    does, which is the whole distinction.
    """
    input:
        bam=rules.starsolo_count.output.bam,
    output:
        cram=f"{OUTDIR}/{{sample}}/{{sample}}.cram",
        crai=f"{OUTDIR}/{{sample}}/{{sample}}.cram.crai",
    container: config["container"]
    threads: config["threads"]
    params:
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io cram --bam {input.bam} --assembly {params.assembly} \
             --out {output.cram} --threads {threads}
        """


rule qc_bundle:
    """Bundle STAR's stats + run logs into one gzipped JSON, then let `temp()` drop the originals.

    Consumes the per-feature stats, the filtered/ tree (only its barcodes.tsv is read -- kept as
    provenance of STAR's default cell call -- but listing the whole tree here is what triggers its
    deletion), and the top-level logs. A `shell:` verb, not a `run:`, so compose's wiring gate sees it.

    **It is also what says the SAMPLE finished, which is why it waits on the sample's deliverables.**
    The count objects and the CRAM are declared here and NOTHING here reads their bytes: the
    dependency is an ordering constraint, and that is the decision rather than a side effect of one.
    A completion record that can be written while a deliverable is still missing is a record of
    nothing, and asking for a sample's QC -- the obvious thing to ask for, since it is what the
    report reads that sample's row from -- has to be the correct thing to ask for. What this cost is
    `rule all`'s enumeration, which is the point: those files were listed there because nothing
    downstream consumed them, and something does now.
    """
    input:
        stats=rules.starsolo_count.output.stats,
        filtered=rules.starsolo_count.output.filtered,
        logs=rules.starsolo_count.output.logs,
        h5ad=rules.solo_to_h5ad.output,
        cram=rules.solo_to_cram.output.cram,
    output:
        f"{OUTDIR}/{{sample}}/{{sample}}{QC_SUFFIX}",
    threads: QC_BUNDLE_THREADS
    params:
        solo=lambda wc: f"{OUTDIR}/{wc.sample}/Solo.out",
        run_dir=lambda wc: f"{OUTDIR}/{wc.sample}",
        features=" ".join(FEATURES),
        assembly=ASSEMBLY,
    shell:
        r"""
        seqforge io qc-bundle --solo-dir {params.solo} --run-dir {params.run_dir} \
             --features "{params.features}" --sample {wildcards.sample} \
             --assembly {params.assembly} --out {output}
        """


def release_genome_segment():
    """Mark the shared genome segment for destruction. ONE command, called by both end-of-run paths.

    Cleanup is OURS, with the site as the backstop. A well-configured scheduler reclaims a killed
    user's IPC segments, but that is site policy rather than SysV semantics -- a segment otherwise
    persists until reboot, which is why STAR ships a `Remove` mode at all -- and seqforge emits a
    Snakefile the USER submits, possibly somewhere else entirely. Calling this from both handlers
    makes the guarantee ours rather than the scheduler's.

    Honest about its reach: a handler runs in the environment that ran snakemake, outside the
    container that has STAR, and it cannot run at all after a SIGKILL. The defensive `Remove` at the
    head of `load_genome` is the half that always holds -- it runs inside the image, and it covers
    exactly the case a handler cannot. Belt and braces, the same shape as clearing `_STARtmp` on
    entry and declaring the outputs that replace it.

    Written once because the two paths release the SAME segment the same way, and two byte-identical
    copies is two chances to fix one of them: the scratch, the redirect and the trailing `|| true`
    have to stay together, and the copy that lost the `|| true` would turn a finished run into a
    failed one. The scratch is the arrangement `load_genome` argues for, on the path where it matters
    most: this command runs when the run is OVER, so a run-file left under the output prefix would
    land in a directory a user is already reading.
    """
    shell(
        "scratch=$(mktemp -d); trap 'rm -rf \"$scratch\"' EXIT; "
        f"STAR --genomeDir {OUTDIR}/index/{ASSEMBLY} --genomeLoad Remove "
        '--outFileNamePrefix "$scratch"/unload_ > /dev/null 2>&1 || true'
    )


onsuccess:
    release_genome_segment()


onerror:
    # The same release on the failing path, and it is the one that matters more: a run that died
    # partway through is exactly the run that leaves a ~30 GB segment resident on a machine with
    # nothing left to detach from it.
    release_genome_segment()
