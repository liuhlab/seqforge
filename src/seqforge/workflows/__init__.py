"""``workflows`` — hand-written, versioned, CI-tested Snakemake modules (NEVER generated).

The composer selects a module by id and emits its ``config.yaml`` + ``units.tsv``; it never writes
rule source. Aligner *environments* and genome *indexes* belong to ``liulab-runtime`` / ``liulab-genome``
and resolve at run time — a module names an env and an assembly id, never a path.

``WORKFLOW_VERSION`` is CalVer and is folded into a manifest's provenance so a compiled config is
bound to the exact module source that will run it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..models.processing import RuntimeEnv

if TYPE_CHECKING:
    from ..kb.schema import Spec

#: CalVer YYYY.M.PATCH; bump when any shipped module's rules/params change.
#: 2026.8.5 — `star.smk` takes ONE mate or two (#274, ADR-0029). `read_layout_kind: paired` becomes
#: `mates` — 1..2 biological mates chosen by ORDER, against the two barcoded kinds which choose by
#: ROLE — and `rule star_count` stops demanding a second one: `input.mate2` is empty when the layout
#: has none, and `--readFilesIn` renders only the mates present, which is STAR's own single-end form.
#: The rename is the change and not decoration on it. The kind is a property of the MODULE, so
#: `map/star` cannot be `paired` for one dataset and `single` for the next; adding a `single` kind
#: would mean a second hand-written module for one aligner, and leaving the name `paired` on a kind
#: that is single-ended half the time would reintroduce exactly what `read_layout_kind` was created to
#: remove — a dispatch key that lies about what it selects. `config["read_files_in"]["mate2"]` is read
#: with `.get`, not a subscript, so `keys_read_by` stops making it a key the composer owes for every
#: bulk dataset — the same optional/required line starsolo.smk draws between `soloBarcodeReadLength`
#: and `soloCBmatchWLtype`. A two-mate library renders the byte-identical command line it did before;
#: the bump is owed anyway, because editing a shipped module invalidates
#: `run_id = H(dataset | processing | kb | workflow)` whatever the edit was.
#: 2026.8.4 — `units.tsv` gains a `lane` column and `fastqs` orders a sample's files by
#: `(run, lane, path)` (#263, ADR-0027). A run is now lane-blind, so a four-lane library is ONE run
#: and the `run` column no longer orders anything within it — lexical path order silently took over
#: the job of pairing the mates, and it holds for bcl2fastq names only by where the read token
#: happens to sit. That failure is invisible: both comma-lists still carry equal read counts, so STAR
#: completes and writes a matrix pairing one lane's barcodes with another lane's cDNA. The column
#: restores the ordering to a fact. The value comes from `resolve.group.lane_of`, the same token the
#: run key dropped, so the module still parses no filename.
#: 2026.8.3 — the two barcoded modules IMPORT the QC artifact suffix they used to spell (#212).
#: **Behaviour is unchanged: only the suffix's ownership moved.** `rule all` and `rule qc_bundle` in
#: starsolo.smk, and `rule fragments_qc` in chromap.smk, declared `.qc.json.gz` / `.fragments.qc.json.gz`
#: as literals; they now read `{QC_SUFFIX}` from `workflows/qc.py` and `workflows/fragments.py`, the
#: modules that WRITE those artifacts. Every declared output resolves to the byte-identical filename
#: — verified by diffing the whole `snakemake -n -p` plan for a composed 10x v3 and a composed 10x
#: Multiome ATAC pipeline across the change — so no rule, no shell command, no config key and no
#: emitted config value differs. The bump is therefore deliberate rather than incidental: editing a
#: shipped module invalidates `run_id = H(dataset | processing | kb | workflow)` whatever the edit
#: was, and a dataset already composed recomposes into a fresh directory with empty results and
#: re-runs. Paying it now is the cheap moment — 2026.8.2 landed days ago and already invalidated
#: everything — and paying it at all is the point: those constants went public with a reader beside
#: the writer, and "adopt them on the next edit for a real reason" is a rule somebody has to
#: remember. A repo-wide check now fails on any shipped `.smk` carrying a suffix its Python owner
#: publishes, so the second spelling cannot come back
#: (`tests/test_repo_invariants.py::test_no_shipped_snakemake_module_restates_a_suffix_its_writer_owns`).
#: 2026.8.2 — `starsolo_count` asks for more memory on a retry, and every memory cap STAR is handed
#: follows the escalated request (#205). The rule declares `retries: STARSOLO_RETRIES` (2) and
#: `resources: mem_mb=escalated_mem_mb(config["mem_mb"], attempt)`, LINEAR — attempt 1 asks for
#: exactly what the recipe asked for, and the third and last attempt gets 3x. `--limitBAMsortRAM` is
#: a SECOND `resources:` entry over the same `attempt` (`bam_sort_ram_bytes`) instead of a constant
#: fraction of `config["mem_mb"]`, so the sort budget rises with the request rather than staying
#: pinned to attempt 1 while the job around it triples. A `resources:` entry and not a `params:` one,
#: measured rather than assumed: `Job.attempt`'s setter clears `_resources` and not `_params`, so a
#: params callable — even one taking `resources`, which snakemake does pass — is expanded once and
#: reused by every retry (traced 750/750/750 against 750/1500/2250 for the same arithmetic as a
#: resource). MEASURED, and the reason 3/4 of the request was never the whole story: `--limitBAMsortRAM`
#: bounds the coordinate sort and nothing else, while STARsolo also holds `readInfo` — 16 B x every
#: INPUT read, `resize(nReadsInput)` over a `{uint64 cb; uint32 umi;}` struct — which none of STAR's
#: eight `--limit*` knobs covers, and there is no `--limitSoloRAM` to add. 215M reads is 3.4 GB of it;
#: the largest worm sample, 2.23 billion reads, is 35.7 GB. And `--limitBAMsortRAM` PERMITS rather
#: than reserves — STAR allocates what the sort needs and refuses only above the cap — so on a large
#: sample the 3/4 rule cheerfully authorises a sort allocation on top of a 36 GB `readInfo`, and the
#: SCHEDULER OOM-kills the job instead of STAR refusing it. What that removes is the illegible death,
#: not the need for memory: a job that still does not fit after 3x fails, loudly and by design, and in
#: the common case — the sort is what did not fit — STAR names the number it needed and exits.
#: Second change, free in memory and in wall-clock alike: `--outSAMmultNmax 1`, a new module literal.
#: STAR wrote and sorted every alignment of a multi-mapping read and `workflows/cram.py` then
#: discarded the secondaries with `-F 0x100` — 198.8M records sorted to retain 162.9M on the measured
#: sample, ~18% of the sort spent on records nothing keeps. `nTrOutWrite = min(P.outSAMmultNmax,
#: nTrOutSAM)` writes only the top-scoring alignment, which is exactly the primary that survives that
#: filter; read off the STAR source, the parameter appears in the SAM/BAM write path and the
#: alignment-ordering code and in NO Solo counting file, so THE COUNTS ARE UNAFFECTED. The retained
#: CRAM is **not** byte-identical, and #205's claim that it was is wrong: for a read with `NH > 1`,
#: `outSAMmultNmax != -1` makes `ReadAlign_multMapSelect.cpp` partition `trMult` so top-scoring
#: alignments come first and then mark `trMult[0]` primary instead of `trBest`, and `HI` is an
#: OUTPUT-ORDER index (`iTrOut + outSAMattrIHstart`, `ReadAlign_alignBAM.cpp`). So a multimapper's
#: retained record always carries `HI:i:1` now, and where several loci tie on score it may be a
#: different one of them — `trBest` breaks the tie on the shorter `gLength`, the partition takes the
#: first in window order. Both are top-scoring, so this is a change of tie-break and not of quality;
#: `NH` still counts every locus (it is computed from `nTrOutSAM`, not the truncated write count), and
#: a uniquely-mapping read is untouched. The version bump already obliges reprocessing, which is what
#: makes a changed CRAM affordable here. `-F 0x100` stays in `cram.py` as a cheap invariant rather
#: than a load-bearing filter. The sort arithmetic left the `.smk` for `workflows/memory.py`
#: (`STARSOLO_RETRIES`, `escalated_mem_mb`, `bam_sort_ram`), where it is importable and unit-tested
#: instead of being a lambda only a real retry ever renders. No new config key — `mem_mb` is the same
#: key it always was, read now as the FIRST attempt's request. The bump invalidates
#: `run_id = H(dataset | processing | kb | workflow)`, which is the axis that exists for "the module
#: changed" (ADR-0005); why the escalation ends in a loud failure rather than a bigger default, and
#: the four alternatives rejected on the way there, is ADR-0023.
#: 2026.8.1 — the retained CRAM carries the BARCODE, and the counts become CellRanger-comparable
#: (#198). Five changes, deliberately in ONE bump: `run_id = H(dataset | processing | kb | workflow)`,
#: so five merges would mean five rounds of run_id invalidation and five reprocessing passes over the
#: same ~920 GiB. (a) `starsolo_count` writes `--outSAMtype BAM SortedByCoordinate` with
#: `--outSAMattributes NH HI AS nM CB UB` — STAR refuses CB/UB in anything but the sorted BAM, and
#: without them the CRAM we retained could not recount, could not be re-quantified, and could not be
#: re-run under another tool; measured 12% SMALLER than the barcode-less CRAM it replaces. It also
#: passes `--limitBAMsortRAM`, 3/4 of `mem_mb` (STAR's default 0 means "reuse the genome allocation",
#: which on a small fixture genome is too small and FATALs). MEASURED, because it is a real operating
#: cost of the barcode: STAR's coordinate sort needs ~160 B per alignment record and REFUSES rather
#: than spilling, so a 215M-read sample wants ~32 GB of sort RAM and a `resources.mem_gb` raised to
#: match. `--outBAMsortingBinsN` does not help (200 bins measured identical, 1000 slightly worse).
#: (b) the CellRanger >=4 equivalence set — `clipAdapterType
#: CellRanger4`, `outFilterScoreMin 30`, `soloUMIfiltering MultiGeneUMI_CR`, `soloUMIdedup 1MM_CR`,
#: `soloCellFilter EmptyDrops_CR` — hardcoded here, because none varies by chemistry and a `SOLO[...]`
#: subscript would silently make each a required key every KB spec must declare. (c) NEW REQUIRED KEY
#: `solo.soloCBmatchWLtype`: it left this module's `soloType` branch for the KB, where the value can
#: differ between two chemistries that share a soloType. (d) `solo_to_cram` no longer sorts (STAR
#: did), so its `samtools sort` — and the undeclared temp files a preempted one leaked — is gone by
#: construction rather than by configuration; the `.fai` fallback moved to a per-call temp dir, so the
#: rule writes nothing snakemake has not declared. (e) `io h5ad` drops every barcode no feature
#: counted: `raw/barcodes.tsv` is the whole whitelist and ~87.6% of it is empty, measured 633.8 MB ->
#: 95.5 MB per deliverable, provably lossless. The mask is the UNION across features, never
#: `X.sum() > 0` — `X` is exonic and `GeneFull` counts introns, so the narrow mask silently deletes
#: the barcodes whose counts are all intronic. Which artifact owns each flag above, and the rule that
#: decides it, is ADR-0022.
#: 2026.7.14 — a SECOND aligner: `map/chromap` (chromap.smk) maps barcoded scATAC to a tabix-indexed
#: `fragments.tsv.gz` (not a count matrix). It resolves its index via liulab-genome's `get_chromap_index`
#: (no GTF — one index per assembly), reads two GENOMIC mates + a barcode read (`read_layout_kind`
#: gains `atac_barcoded`), declares its own one-key parse namespace (`barcode_whitelist`), and serves
#: modalities {atac, multi}. STARsolo/star behaviour is byte-identical — this is purely additive, so a
#: version bump for the new module, not a change to the old ones.
#: 2026.7.13 — both mapping rules (`starsolo_count` in starsolo.smk, `star_count` in star.smk) clear
#: STAR's `_STARtmp` (`rm -rf {params.prefix}_STARtmp`) before invoking STAR, so a (re)run is
#: preemption-safe: a preempted STAR leaves `_STARtmp` behind, STAR aborts a rerun if it exists, and
#: snakemake cannot remove it (undeclared output). No new config key.
#: 2026.7.11 — starsolo.smk gains an always-on finalize: `starsolo_count` now declares its stats,
#: logs, filtered/ tree and BAM as `temp()` outputs; new `solo_to_cram` (BAM -> sorted CRAM via
#: `seqforge io cram`) and `qc_bundle` (stats+logs -> one gzipped JSON via `seqforge io qc-bundle`)
#: consume them, so the raw matrices, filtered copies, scattered stats and BAM are all deleted once
#: the retained deliverables (h5ad, cram, qc.json.gz) land. No new config key (reads only the
#: already-required `genome.assembly` + `threads`).
#: 2026.7.7 — `genome_index` (starsolo.smk + star.smk) now *resolves* the STAR index via
#: liulab-genome's `get_star_index` (a lookup that raises if none is built) instead of
#: `build_star_index` (build-if-missing). Building is liulab-genome's concern, done ahead of the run;
#: the pipeline consumes the artifact and never decides when it is built. No STAR on PATH needed here.
#: 2026.7.6 — `starsolo_count` passes `--soloBarcodeReadLength` when the chemistry declares it. 10x
#: v2/v3/v3.1 set it to 0, which disables STARsolo's default check that the barcode read is exactly
#: CB+UMI long — their R1 is routinely sequenced longer (a 150 nt R1) and the default FATALs on the
#: excess. Read with `SOLO.get(...)` so it stays OPTIONAL: a chemistry that omits it (SPLiT-seq) keeps
#: STAR's default and the flag is not a `required_config` key it would then have to emit.
#: 2026.7.5 — `starsolo_count` declares `container:`, so the recorded env name is load-bearing at
#: last instead of emitted and ignored. `config["env"]` is REPLACED by `config["container"]`: the
#: manifest carries the env name, and the config carries this machine's rendering of it (the
#: machine-independence boundary, same as fastq paths). Only the STAR rule gets one — `genome_index` is a `run:` block,
#: and Snakemake wraps containers in `shell.py`, so a `container:` there is silently ignored.
#: 2026.7.4 — `starsolo_count` declares its per-feature matrices as NAMED outputs instead of
#: `directory(Solo.out)`, and `solo_to_h5ad` packages them: the default target is the deliverable.
#: 2026.7.3 — `required_config` is COMPUTED from the module source instead of typed beside it, so
#: over- and under-declaration are both impossible rather than one being tested. `units_tsv` joins it
#: (the composer emits it now; no wrapper injects it). `read_layout_kind` replaces the hardcoded
#: `module == "map/starsolo"` branch in the composer.
#: 2026.7.2 — starsolo's required_config gains the four soloCB/UMI keys starsolo.smk has always
#: dereferenced and never declared. The contract was wrong, not the module.
#: 2026.7.1 — star.smk hardcodes --outSAMtype (it is a module detail, and starsolo.smk always
#: hardcoded it); required_config gains primary_feature and drops bulk.outSAMtype.
WORKFLOW_VERSION = "2026.8.5"

_MODULE_DIR = Path(__file__).parent

#: liulab-runtime's published image. **A reference to their artifact, never a definition of one**:
#: we name a tag they build and push, and this repo still contains no conda YAML, no
#: Dockerfile, and no aligner in any dependency table. `align-rna` is where STAR comes from.
RUNTIME_IMAGE = "ghcr.io/liuhlab/liulab-runtime"

#: How liulab-runtime names a prebuilt Singularity image. Read off their own `build-sifs.sh` on
#: 2026-07-15 (`$LIU_LAB_PACKAGES/liulab-runtime_<env>.sif`), not remembered — the four files there
#: are exactly the four `RuntimeEnv` names, which is an independent confirmation of that literal.
_SIF_NAME = "liulab-runtime_{env}.sif"


def container_uri(env: RuntimeEnv, sif_dir: str | Path | None = None) -> str:
    """The container image for ``env``: a ghcr tag, or a prebuilt ``.sif`` if one is on this machine.

    ``docker://`` by default, which is portable and needs no setup — Snakemake pulls it. But a
    compute node that cannot reach ghcr.io cannot pull anything, and the lab already builds these
    images ahead of time, so ``sif_dir`` names where. Missing dir or missing file falls back to the
    ghcr tag rather than emitting a path to nothing: a config naming an absent SIF fails at run time
    on a node, while the tag at least tries.

    This is a **machine fact**, so it belongs in the config and never in the manifest — same
    boundary as ``--fastq-dir`` and ``--onlist-dir``, and the same escape hatch for the same reason.
    """
    if sif_dir is not None:
        sif = Path(sif_dir) / _SIF_NAME.format(env=env)
        if sif.is_file():
            return str(sif.resolve())
    return f"docker://{RUNTIME_IMAGE}:{env}"


@cache
def keys_read_by(snakefile: Path) -> frozenset[str]:
    """The dotted config keys a module actually reads, **derived from its source**.

    Two forms, because the modules use both: `config["a"]["b"]` directly, and the indirection
    `params: solo=config["solo"]` followed by `{params.solo[soloCBlen]}` in the shell block.

    Comments are stripped first, and that is not fussiness — starsolo.smk's own header prose says
    "every chemistry-defining knob arrives via `config["solo"]`", which a naive scan reads as a bare
    read of the whole block. A check that cries wolf gets deleted.
    """
    code = "\n".join(line.split("#")[0] for line in snakefile.read_text().splitlines())
    keys: set[str] = set()

    # A bare `<name> = config["<section>"]` binds the whole block to a name. Track those, including
    # one rebinding hop (`SOLO = config["solo"]` at module level, then `solo=SOLO` in a params
    # block), because that chain is exactly how the shell reaches `{params.solo[soloType]}`.
    # The lookahead matters: `ASSEMBLY = config["genome"]["assembly"]` is a nested read, not a
    # binding, and must fall through to the direct scan below.
    bound: dict[str, str] = dict(re.findall(r'(\w+)\s*=\s*config\["(\w+)"\](?!\[)', code))
    for name, src in re.findall(r"^\s*(\w+)\s*=\s*(\w+)\s*,?\s*$", code, re.M):
        if src in bound:
            bound.setdefault(name, bound[src])

    for name, section in bound.items():
        # `{params.<name>[<key>]}` in a shell block, or `<NAME>["<key>"]` in Python.
        subscripts = set(re.findall(rf"\{{params\.{name}\[(\w+)\]\}}", code)) | set(
            re.findall(rf"""\b{name}\[["'](\w+)["']\]""", code)
        )
        # Subscripted -> it is a block alias and each subscript is the real read. Never subscripted
        # -> it was a scalar read all along (`OUTDIR = config["outdir"]`), so the section IS the key.
        keys |= {f"{section}.{k}" for k in subscripts} or {section}

    # Direct reads: config["a"]["b"] -> a.b | config["a"] -> a. Binding sites are already accounted
    # for above, so drop them here rather than double-count the block as a bare key.
    direct = re.sub(r'\w+\s*=\s*config\["\w+"\](?!\[)', "", code)
    for section, sub in re.findall(r'config\["(\w+)"\](?:\["(\w+)"\])?', direct):
        keys.add(f"{section}.{sub}" if sub else section)

    return frozenset(keys)


#: The parse-param namespace a ``map/starsolo`` backend may declare — every key says how to **parse**
#: reads, and each is decided by bytes. The line is parse vs. count: what to COUNT (``soloFeatures``,
#: ``quantMode``) is *intent* and belongs to the processing manifest, where a user may instruct it and a
#: gate may check it. ``soloFeatures`` once sat here and cost a measured **40.7 % of a nuclear library**
#: — 10x 3' v3.1 chemistry is byte-identical for cells and nuclei, so counting was never a chemistry
#: property. Keeping this namespace **per pipeline** (a ``Pipeline.parse_keys`` field, not one global
#: set) is what lets a second aligner declare its own parse knobs without widening STARsolo's, so
#: "a user instruction contradicts the observed bytes" stays structurally inexpressible per namespace.
_STARSOLO_PARSE_KEYS: frozenset[str] = frozenset(
    {
        "soloType",
        "soloCBstart",
        "soloCBlen",
        "soloUMIstart",
        "soloUMIlen",
        "soloCBwhitelist",
        "soloCBposition",
        "soloUMIposition",
        "soloStrand",
        "soloAdapterSequence",
        "soloBarcodeReadLength",
        # How a read barcode is matched back to the whitelist — a parse knob by the same test as the
        # rest: it is decided by the chemistry's own vendor pipeline, never by what a user wants
        # counted. It was a `soloType` branch in starsolo.smk (`1MM` for Complex, STAR's default for
        # Simple) until #198, and a two-way branch cannot express what the KB already needs: Parse
        # Evercode and BD Rhapsody are BOTH `CB_UMI_Complex` and take different values (`EditDist_2`
        # vs `1MM`). Three answers, so it moves to the artifact that has one row per chemistry. Which
        # values are legal depends on the soloType (compose's params gate enforces the pairing).
        "soloCBmatchWLtype",
    }
)

#: chromap's parse namespace — the byte-decided knobs a ``map/chromap`` backend may declare. Just the
#: barcode whitelist: chromap corrects the cell barcode against it (like STARsolo's ``soloCBwhitelist``),
#: and it resolves through the same ``{onlist:<alias>}`` mechanism to a materialized path. Everything
#: else chromap needs is either a fixed module detail (the ``--preset atac`` mode, hardcoded in
#: chromap.smk the way star.smk hardcodes ``--outSAMtype``) or read geometry the manifest already states
#: (which file is the barcode read arrives via ``read_files_in``, not a parse param). The namespace is
#: DISJOINT from STARsolo's, which is what keeps "a user instruction contradicts the bytes" inexpressible
#: per pipeline: a chromap backend is policed against exactly this set, a starsolo backend against ``solo*``.
_CHROMAP_PARSE_KEYS: frozenset[str] = frozenset({"barcode_whitelist"})

#: How a module wants its reads handed to the aligner — a CLOSED vocabulary, extended deliberately:
#:
#: - ``barcoded``      — ``{cdna, barcode}``, chosen by ROLE (a barcoded single-cell RNA chemistry).
#: - ``mates``         — ``{mate1}`` or ``{mate1, mate2}``, chosen by ORDER (a bulk library, single-
#:   or paired-end).
#: - ``atac_barcoded`` — ``{gdna1, gdna2, barcode}``, chosen by ROLE (scATAC: two genomic mates and a
#:   separate barcode read — chromap's ``-1``/``-2``/``-b`` shape).
#:
#: A typed, visible choice rather than the old ``spec.backend.module == "map/starsolo"`` string
#: compare, in which every module that was not starsolo silently fell into the bulk mate1/mate2
#: branch and emitted a wrong command line. A third module must pick a kind, or add one.
#:
#: **``mates`` is 1..2 and not exactly 2, and the name moved for that reason** (ADR-0029). A kind is a
#: property of the MODULE, so ``map/star`` cannot be ``paired`` for one dataset and ``single`` for the
#: next; a ``single`` kind would buy a second hand-written module for one aligner. Widening the one
#: kind is what avoids that, and then the old name would have been a dispatch key that lies about what
#: it selects — the exact defect this field exists to remove.
#:
#: Named here rather than written inline on the field, because the composer's dispatch and the params
#: gate's cross-check are two independent re-derivations of one mapping and must be annotated against
#: the SAME set. Two separately spelled ``Literal``s agree right up until one of them gains a kind.
ReadLayoutKind = Literal["barcoded", "mates", "atac_barcoded"]


@dataclass(frozen=True)
class WorkflowModule:
    """One selectable pipeline: its id, version, runtime env, Snakefile, and per-pipeline contract.

    The pipeline registry's citizen. Beyond identity (``name``/``version``/``snakefile``) it declares
    the properties that used to be STARsolo-hardwired globals scattered across ``compose``/``policy``:
    the runtime ``env``, the ``read_layout_kind``, the ``parse_keys`` namespace a KB backend may declare,
    and the ``serves_modalities`` the assay↔pipeline adapter (:func:`resolve_pipeline`) checks. The
    aligner name and the config block are *derived*, never declared twice.
    """

    name: str
    version: str
    env: RuntimeEnv
    snakefile: Path
    #: How this module wants its reads handed to the aligner — see :data:`ReadLayoutKind`.
    read_layout_kind: ReadLayoutKind
    #: The parse-param namespace this pipeline's KB backends may declare (byte-decided knobs). Empty for
    #: a bulk pipeline that takes no parse params. Per pipeline, so a chromap backend declares chromap's
    #: parse knobs and a starsolo backend declares ``solo*`` — each gated against its own namespace.
    parse_keys: frozenset[str] = frozenset()
    #: Which assay modalities this pipeline serves. The adapter refuses to bind a spec whose modality is
    #: not here, so an RNA chemistry can never be composed against an ATAC-only pipeline (or vice versa).
    serves_modalities: frozenset[str] = frozenset({"rna"})

    @property
    def aligner(self) -> str:
        """The aligner name recorded in ``processing.aligner`` — derived from the module id.

        ``map/starsolo`` → ``starsolo``, ``map/star`` → ``star``, ``map/chromap`` → ``chromap``. This
        was a ``_ALIGNER_FOR_MODULE`` dict whose every entry equalled this ``rsplit`` fallback — a
        mirror of the module ids that could only ever drift from them. One rule, read off the id.
        """
        return self.name.rsplit("/", 1)[-1]

    @property
    def required_config(self) -> tuple[str, ...]:
        """Dotted config keys the module reads — the composer must emit every one.

        **Computed from the module source, never declared.** This was a hand-written tuple, checked in
        one direction against a scanner that lived in the test suite. It under-declared the four
        soloCB/UMI keys `starsolo.smk` has always dereferenced (a `KeyError` on a compute node, long
        after compose exited 0), and it over-declared `primary_feature` and `env`, which no rule
        reads. Both directions now close by construction: there is one list, and the module source is
        it. A hand-maintained list of what the code does is a comment with a tuple's syntax.

        Deriving is only safe because the module now *executes*: `kb e2e` runs this Snakefile against
        real reads and a ground-truth matrix, so a key this scanner misses fails loudly there. The two
        are complementary — `kb e2e` exercises one chemistry's branch, this covers both statically.
        """
        return tuple(sorted(keys_read_by(self.snakefile)))

    @property
    def param_block(self) -> str:
        """Which config block carries this module's aligner params. **Read off the module source.**

        `starsolo.smk` dereferences `config["solo"]`; `star.smk` dereferences `config["bulk"]`. That
        is not a preference anyone declares — it is what the file does — so it is derived from
        `required_config`, which is itself scanned out of the module.

        It was `"solo" if spec.backend.module == "map/starsolo" else "bulk"`, the last surviving
        string compare against a module name, and it is the same bug `read_layout_kind` was created
        to kill: every module that is not starsolo silently means bulk. A third module would have had
        its params written into a `bulk:` block it never reads, and the params gate — which uses this
        same function — would have agreed with the composer, because both were wrong in the same
        direction. Two things wrong identically is what a shared bug looks like from inside a test.

        A module that reads neither block, or both, raises. That is a module whose config contract we
        do not understand, and guessing would be how the wrong params reach an aligner.
        """
        blocks = sorted(
            {k.split(".")[0] for k in self.required_config} & {"solo", "bulk", "chromap"}
        )
        if len(blocks) != 1:
            raise ValueError(
                f"{self.name} reads {blocks or 'no'} aligner-param block(s) in its config; expected "
                f"exactly one of solo/bulk/chromap. A module whose contract is unreadable must not be "
                f"guessed at — add the block it reads, or teach `param_block` the new shape."
            )
        return blocks[0]


MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        read_layout_kind="barcoded",
        parse_keys=_STARSOLO_PARSE_KEYS,
    ),
    "map/star": WorkflowModule(
        name="map/star",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star.smk",
        read_layout_kind="mates",
    ),
    "map/chromap": WorkflowModule(
        name="map/chromap",
        version=WORKFLOW_VERSION,
        env="align-dna",
        snakefile=_MODULE_DIR / "map" / "chromap.smk",
        read_layout_kind="atac_barcoded",
        parse_keys=_CHROMAP_PARSE_KEYS,
        serves_modalities=frozenset({"atac", "multi"}),
    ),
}


def get_module(name: str) -> WorkflowModule:
    """Return the workflow module registered under ``name`` (raises ``KeyError`` if unknown)."""
    try:
        return MODULES[name]
    except KeyError as exc:
        known = ", ".join(sorted(MODULES))
        raise KeyError(f"unknown workflow module {name!r}; known: {known}") from exc


def parse_keys_for(module: str) -> frozenset[str]:
    """The parse-param namespace a backend on ``module`` may declare (raises for an unknown module).

    The single source of truth for the parse/count line — consulted by the KB DSL validator
    (``Backend._only_parse_keys``) and the composer's ``params_gate`` alike, so both police one namespace
    per pipeline rather than a global set that every pipeline had to share.
    """
    return get_module(module).parse_keys


def resolve_pipeline(spec: Spec) -> WorkflowModule:
    """Bind an identified chemistry to the pipeline that runs it — the assay↔pipeline adapter.

    ``get_module`` plus one invariant: the spec's modality must be one the pipeline serves. That check
    is the whole reason the adapter exists — it makes "an ATAC chemistry composed against STARsolo"
    a loud refusal at compose time instead of a wrong command line, the same class of silent
    fall-through that ``read_layout_kind`` and ``param_block`` were built to kill. Raises ``KeyError``
    (which the composer surfaces as a ``ComposeError``) for an unknown module or an unserved modality.
    """
    module = get_module(spec.require_backend().module)
    modality = spec.identity.modality
    if modality not in module.serves_modalities:
        raise KeyError(
            f"pipeline {module.name!r} serves modalities {sorted(module.serves_modalities)}, not "
            f"{modality!r} (chemistry {spec.identity.id!r}); a chemistry must be composed against a "
            f"pipeline that serves its modality"
        )
    return module


def list_modules() -> list[str]:
    return sorted(MODULES)


__all__ = [
    "WORKFLOW_VERSION",
    "RUNTIME_IMAGE",
    "ReadLayoutKind",
    "WorkflowModule",
    "MODULES",
    "container_uri",
    "get_module",
    "keys_read_by",
    "list_modules",
    "parse_keys_for",
    "resolve_pipeline",
]
