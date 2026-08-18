"""``workflows`` — hand-written, versioned, CI-tested Snakemake modules (NEVER generated).

The composer selects a module by id and emits its ``config.yaml`` + ``units.tsv``; it never writes
rule source. Aligner *environments* and genome *indexes* belong to ``liulab-runtime`` / ``liulab-genome``
and resolve at run time — a module names an env and an assembly id, never a path.

``WORKFLOW_VERSION`` is CalVer and is folded into a manifest's provenance so a compiled config is
bound to the exact module source that will run it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..models.processing import RuntimeEnv

if TYPE_CHECKING:
    from ..kb.schema import Spec

#: CalVer YYYY.M.PATCH; bump when any shipped module's rules/params change.
#: 2026.8.17 — a chimeric SMART-seq3 plate produces a complete, readable, single-species result set,
#: and the run directory and the report tell the truth about what happened (#429). ONE bump for eight
#: slices, deliberately: `run_id` folds this constant and not the module bytes, so paying it per slice
#: would orphan a pipeline directory per slice and re-run a two-arm cluster pilot to prove each one.
#: **The split accepts healthy data** (#435). It refused 16 of 16 cells of a mapped pilot on an
#: end-of-run check asserting each output kept as many first mates as second — true premises, false
#: conclusion, because where only one mate aligns the aligner omits the other and the survivor is a
#: mapped primary alignment with no partner beside it. Such a survivor carries the mate-unmapped flag,
#: so it is a singleton by construction: each side's own come off that side and the PAIRED REMAINDER
#: is what must balance. The count is derived a second, independent way — placeless records sit at
#: their live mate's coordinates, so they name a Component too — and the two are compared. Still a
#: stateless per-record filter: two more counters, no buffer (ADR-0053).
#: **The keep rule widens to mapped, primary.** Multiply-placed fragments are routed by their
#: representative record's Component and MARKED rather than dropped, by the hit-count tag they already
#: carry, so the counter separates the populations with no intermediate artifact and no Chimera
#: concept. `multimapping` stops being a drop category; the summary gains each Component's
#: multiply-placed share and its singleton count.
#: **The aligner writes what it could not place** — `--outSAMunmapped Within` on both plate twins, so
#: the counter's first fate stops being structurally zero on a plain run (#434, ADR-0049 amended).
#: Not `Within KeepPairs`: that affects unsorted output only and these modules write sorted.
#: **The retained archive is partitioned by mappability** (#436, ADR-0054). `umi_to_cram` is gone;
#: `unique_to_cram` and `multiplaced_to_cram` replace it on both twins. On the chimeric twin the
#: unique half is per Component off that Component's split BAM against that Component's own reference
#: — single-species names, order, lengths and binary dictionary — while the multiply-placed half is
#: ONE Component-blind file per cell in Chimera coordinates, cut from the pre-split BAM. Together they
#: are exactly every primary mapped record, which is why the whole-Chimera archive is gone rather than
#: kept beside them. `seqforge io cram` gained the record selection that makes this one verb
#: (#433) and stamps the caveat as an `@CO` line on the ambiguous half.
#: **One QC artifact per cell on both twins** (#438), absorbing the extraction summary, the aligner's
#: final log — now a DECLARED output — its two progress logs, a junction SUMMARY and, on the chimeric
#: twin, the split summary. Every original is `temp()`, which having a consumer is what makes legal.
#: The junction TABLE is not stored: same artifact kind at eighty times the droplet arity is not the
#: same trade-off, and the droplet bundle is untouched. What marks a cell finished moved onto the
#: bundle, which is downstream of everything and so cannot report a cell finished the moment its
#: aligner log appeared — the failure mode the pilot's clean-looking page was made of.
#: **No aligner scratch inside the deliverable** (#431). The shared genome load and release write
#: their run-files into a `mktemp -d` the same shell block destroys, on all four modules that load an
#: index: a mechanism that cannot leak beats one that must be configured correctly. `map/star` keeps
#: its junction table as a real output — analyzable at bulk depth, unlike a cell's — and sweeps the two
#: progress logs nothing reads.
#: **Off-module, riding the same bump:** the counter gains a gene-body, UMI-deduplicated
#: multiply-placed layer, a per-cell locus-count distribution and per-cell saturation on the object it
#: already writes, with the primary matrices byte-identical (#437); the chimeric twin gains the
#: per-Component fan-in reader those columns need to reach a page (#439); and a report says `finished`
#: or `failed` against the deliverables each module declares, with no third answer (#432, ADR-0052).
#: 2026.8.16 — the plate route declares the read group its records name (#416). It was
#: shipping INVALID SAM and had been since the uBAM arrived: `umite`'s extractor writes
#: `@RG ID:<cell> SM:<cell>` into the uBAM and stamps `RG:Z:<cell>` on every record, and
#: `--readFilesSAMattrKeep All` carried that tag through the aligner — but STAR builds its output
#: header from the genome and its own parameters and inherits NOTHING from an input BAM, so every
#: aligned record named a read group its header never introduced. The specification forbids that.
#: samtools and pysam tolerate it, Picard and GATK refuse the file outright, and the per-cell CRAM is
#: the RETAINED artifact — so the malformed file is the one that ships to whoever receives the data.
#: **THE FIX IS TWO FLAGS AND THE SECOND ONE IS NOT OPTIONAL.** `--outSAMattrRGline ID:<sample>
#: SM:<sample>` is STAR's only input to an `@RG` header line, and setting it also appends `RG` to the
#: output attribute order on STAR's own initiative — `RG` is not a word `--outSAMattributes` accepts,
#: so header and tag are one decision and cannot be taken separately. That is precisely what made the
#: keep list load-bearing: STAR writes its own attributes and THEN appends the kept input tags,
#: de-duplicating against nothing (`ReadAlign_alignBAM.cpp` calls `bamAttrArrayWriteSAMtags`, which
#: filters on the keep list alone), so `All` plus the flag would have written `RG` on a record TWICE
#: — a worse file than the one being fixed. The two plate modules therefore keep `UB` by name instead
#: of `All`; the input `RG` is dropped and STAR's is the only one. Nothing is lost, because both
#: spell the same `{wildcards.sample}` and the extractor's uBAM is unchanged.
#: **THE TWO PLATE MODULES ONLY, AND THE OTHER TWO ARE LEFT ALONE ON PURPOSE.** `map/star` and
#: `map/starsolo` read FASTQ, hand STAR no input tags, and so stamp no `RG` at all: their files are
#: not malformed, they merely carry no library provenance — a usability gap the GATK family also
#: refuses, and a DIFFERENT defect from this one. Giving them a read group would move the bytes of
#: every alignment and every retained CRAM on two routes that have nothing wrong with them, and the
#: re-keying that would pay for it does not happen by itself: `run_id` folds the `workflow_version`
#: STAMPED IN THE RECIPE, not the live constant, so an existing `processing.yaml` recompiles to the
#: same id and into the same directory as before, now under a module whose command line has moved.
#: That is the ordinary cost of editing a shipped module and it is worth paying to fix invalid SAM;
#: it is not worth paying to add a field to files nobody has complained about. Whoever wants it on
#: the FASTQ routes should ask for it, and re-stamp their recipes when they do.
#: **WHICH OUTPUT MOVES. EVERY ALIGNMENT THE TWO PLATE MODULES WRITE**, and nothing else — the
#: per-cell BAMs of `map/star-umi` and of the chimera twin, and the retained CRAMs made from them.
#: Each gains an `@RG` line in its header and an `RG:Z:` on every record. `map/star`, `map/starsolo`
#: and `map/chromap` are untouched, byte for byte. Counts are untouched everywhere too:
#: `--outSAMattrRGline` reaches the SAM/BAM write path and no counting path, and the plate counter
#: reads `UB` and `NH`, neither of which moves. The chimera splitter and the CRAM converter inherit
#: the line for free and were not edited — the splitter copies every non-`@SQ` header line verbatim,
#: the converter's `awk` passes `/^@/` through — so the split BAMs and every CRAM carry it without
#: either learning what a read group is.
#: A SEPARATE entry from 2026.8.15 rather than an extension of it, deliberately: that entry's "which
#: output moves" says NONE, and it is true of the twin's arrival. Folding this in would make it false.
#: 2026.8.15 — a FIFTH module, `map/star-umi-chimera`: the chimera-aware twin of the plate pipeline
#: (#423, spec #419). A **Chimera** is one reference built from several **Component** assemblies whose
#: chromosome names carry a component suffix, and until now seqforge would compile against one and
#: produce nothing anyone could use — a BAM whose every chromosome is spelled wrong for every tool the
#: user owns, and one matrix counted against the merged annotation with both organisms' genes as its
#: columns and no number anywhere saying how much of the library was which. The twin runs the same
#: per-cell chain and then, per cell, ONE new step: `rule split_chimera` shells out to `seqforge io
#: split-chimera` and writes one BAM per Component, each restored to the chromosome names, `@SQ` order
#: and lengths a run against the bare Component would have written, plus a non-`temp()` per-cell split
#: summary. `rule umi_count` then carries one `{component}` wildcard over `config["genome"]["components"]`
#: — read at PARSE time, so a dry run needs no built reference — and renders `--component` in place of
#: `--annotation`, which makes each Component counted against what IT contributed to the merge. `rule
#: all` demands each Component's `.h5ad` BY NAME, because a rule whose output is a folder is satisfied
#: by a folder, which is how a counting job that wrote two Components of three exits 0.
#: **THE SPLIT SITS BESIDE THE CRAM, NOT UPSTREAM OF IT.** `umi_to_cram` reads the pre-split chimeric
#: BAM, so a chimeric archive keeps every multimapper and is strictly MORE complete than a
#: single-assembly one, and peak disk falls slightly because the chimeric BAM is freed once both
#: consumers finish instead of living until the fan-in. `--outSAMmultNmax 1` STAYS: the split's keep
#: rule never asks where a multimapper's other loci are.
#: **A FULL STANDALONE COPY, AND THAT IS FORCED.** Composition copies exactly one `.smk` into the run
#: directory, so an `include:`d fragment would be neither copied nor eligible as the default target,
#: and the file's basename must equal the module id's tail because module identity is inverted out of
#: it. Four things differ out of ~580 lines — the header, `rule all`, `split_chimera` and `umi_count`
#: — and what keeps the copies in step is DERIVED from this registry rather than typed beside it: the
#: shared-genome lifecycle sweep, the wiring gate, the config-key scanner and the verb-existence check
#: each pick the twin up because it is registered. There is deliberately no same-ness test against the
#: base; its subject would be source text.
#: **SELECTION IS THE BASE'S DECLARATION AND NOTHING ELSE.** `map/star-umi` names the twin as its
#: `chimeric_variant`, so compose swaps where it used to refuse; the other three still declare nothing
#: and a chimeric assembly on them is still a hard refusal naming the module. The twin is reachable no
#: other way — the guard set is derived from this registry and a KB backend naming a variant is
#: refused at load — and its `parse_keys` stay EMPTY and must.
#: **WHICH OUTPUT MOVES. NONE, and this is argued rather than benchmarked.** A chimeric run selects a
#: DIFFERENT `.smk`; a non-chimeric compile reaches one new call whose only outcome on that path is
#: `None`; the twin is a NEW FILE, so no existing module is edited and no existing rule, param or
#: command line moves; the emitted config is byte-identical, because the one new key
#: (`genome.components`) is emitted CONDITIONALLY, the same shape `primary_feature` already has; no
#: recipe migration is owed; and **no existing pipeline directory moves** even though a fifth module
#: bumps this stamp, because `run_id` folds the `workflow_version` recorded in the recipe rather than
#: the live one. Two modules also cannot collide on one `run_id` by construction: the module id is in
#: no hash at all, a chimeric run re-keys via the assembly inside the processing hash, and the module
#: is a pure function of `(spec, assembly)` with both already hashed.
#: **EVERY PER-COMPONENT FIGURE IS OVER UNIQUELY-PLACED READS ONLY**, which is the split's keep rule:
#: a read ambiguous ACROSS organisms is indistinguishable from a within-organism repeat, so a
#: bacterial fraction read off these matrices is a LOWER BOUND. `unmapped` and `multimapping`
#: consequently read structurally zero in a chimeric `.h5ad` — those reads leave one rule earlier —
#: and the per-cell split summary is where they now live, which is why it ships and why the report
#: reads it.
#: **THE MEMORY FIGURES ARE THE BASE'S, CARRIED OVER IDENTICAL, AND HONESTLY UNMEASURED.** A chimeric
#: index is larger than any one Component's and nobody has measured one; the twin's header says so
#: rather than inventing a multiplier.
#: 2026.8.14 — `rule umi_count` uses the threads it asks the scheduler for (#397). It requested
#: `config["threads"]` and handed the verb none of them, so a plate-wide counting job over 784 cells
#: — or the 1440 the counter sizes for — ran on ONE core while the rest of the allocation sat idle.
#: The rule now renders `--threads {threads}`, the verb takes it, and `count_plate` forks a worker
#: per core over cells that are independent by construction.
#: **WHICH OUTPUT MOVES. NONE, and that is the whole invariant** — this is one item of a performance
#: series in which a moved count is a bug and not a result. The cells were always counted
#: independently; what changed is how many at once. Rows are collected BY INDEX and never by
#: completion, so the h5ad's row order is still the order this rule lists the cells in, and the same
#: plate counted twice is still byte-identical.
#: **FORK OR SERIAL, NEVER SPAWN.** A forked worker inherits the annotation, so nothing is
#: serialised — which is the opposite of the 47.5 MB-per-worker pickle the counter's header declines
#: to reproduce, not a return to it. `spawn` would re-import the module and pickle the annotation
#: into every worker on every cell, so where a platform does not offer fork the plate is counted on
#: one core instead: slower is a cost, a pickle per cell is a design.
#: **THE MEMORY REQUEST IS UNCHANGED, and it was re-checked rather than assumed.** A worker's
#: resident growth is a CEILING and not a rate — ~75 MB of copy-on-write on the interned gene sets,
#: reached inside the first twenty thousand fragments of the first cell and flat at two million — so
#: the default eight-wide fan-out adds ~0.6 GB against a request that floors at 8 GB. The numpy and
#: `array.array` buffers are untouched by refcounting and stay genuinely shared. Measurements:
#: `docs/research/umite-performance-2026-08-11.md`.
#: The bump is owed by the `.smk` edit, and a `run_id` is its whole cost.
#: 2026.8.13 — `rule umi_extract` writes what the extraction MEASURED to disk, not only to stdout
#: (#353). It gains a second output, `<sample>.umi-extract.json`, declared from `EXTRACT_SUFFIX` in
#: the module that writes it and deliberately NOT `temp()`; the verb takes `--summary` and writes the
#: same payload it already printed, plus the geometry it ran under and the seqforge version.
#: **WHY A FILE AT ALL.** The uBAM is `temp()`, so the moment the aligner and the CRAM converter have
#: consumed it every record is gone — and with them the only evidence of how many fragments carried a
#: tag. That share is not incidental: the fraction of UMI-containing reads is a tunable property of
#: the tagmentation, published across 6.9–70.5% over five libraries, so it is the single best
#: per-cell readout of whether the chemistry behaved, and a cell at 2% and a cell at 28% are a bench
#: problem and a normal run that nothing downstream can tell apart. Printed to stdout, the only
#: surviving copy was whatever captured the workflow's output — on a cluster, a scheduler log
#: somebody rotates. The offsets histogram is the same evidence one level down: where the tag
#: actually started, so a shifted distribution is a primer or trimming problem no count matrix would
#: explain. `seqforge report` now reads the file the way it already reads `Log.final.out`, which is
#: what makes `map/star-umi` the first module with TWO per-sample artifacts — and the first with one
#: that is NOT evidence a sample finished, so a `SampleArtifact` says which it is and a plate whose
#: extraction has outrun its aligner reports "3 of 1440" rather than a green "all finished".
#: **WHICH OUTPUT MOVES.** No FILE already produced. The uBAM is byte-identical and every alignment
#: and count is unchanged. Stdout keeps every key it had and gains three — the geometry, the seqforge
#: version and where the summary went — because the printed payload and the written one are one
#: object, and two accounts of one extraction is the drift this artifact exists to end.
#: The bump is owed by the `.smk` edit, and a `run_id` is its whole cost.
#: 2026.8.12 — every STAR workflow loads ONE copy of the genome per run and shares it, instead of one
#: workflow doing so and two loading a private copy per job (#379). `map/star` and `map/starsolo` each
#: gain the arrangement `map/star-umi` has had since 2026.8.6: a `load_genome` rule that defensively
#: clears a stale segment and then loads the index (`--genomeLoad LoadAndExit`), touching a flag that
#: is deliberately NOT `temp()` — deleting it would tell snakemake the load never happened, and the
#: rerun would reload a segment that is already resident; a dependency edge from the mapping rule onto
#: that flag; `--genomeLoad LoadAndKeep` on the mapping invocation; and one `release_genome_segment()`
#: helper called from BOTH `onsuccess:` and `onerror:`, because a run that died mid-way is exactly the
#: run that strands tens of gigabytes on the machine. The clear comes BEFORE the load: marking a stale
#: segment for destruction after loading is a load that inherits it.
#: **WHAT IT BUYS.** STAR's index is per-process and resident for the life of the job, so N samples
#: mapping at once cost N copies of it — six droplet samples against a ~31 GB human index is ~186 GB
#: of index where ~31 GB would do. A composed pipeline runs on ONE machine (ADR-0051), which is both
#: what makes one segment attachable by every job and what makes the multiplication real. The plate's
#: own argument was different and is now stated as such: repeated LOADING (1440 per-cell loads of a
#: ~30 GB index is ~40 TB of I/O to align 54 GB of FASTQ) is compatible with, but says nothing about,
#: concurrent RESIDENCY. Its docstring no longer asserts the other two modules should not do this.
#: **WHICH OUTPUT MOVES.** None. `--genomeLoad` selects where the index lives, not how it is used, and
#: the alignments and counts are unchanged. What moves is a machine's peak footprint and a new
#: precondition: STAR refuses `--limitBAMsortRAM 0` under a shared copy, which is why #377 (bulk's
#: sort cap) had to land first — the refusal fires before the genome directory is read, so it would
#: have been every bulk sample on the first attempt rather than a degradation.
#: **THE PATTERN IS COPIED, NOT FACTORED.** A workflow file is a standalone hand-written artifact:
#: composition copies exactly one of them into the run directory, and an `include:`d fragment would be
#: neither copied nor eligible as the default target. Three copies of a lifecycle that must stay in
#: step is the real cost, and the test that keeps them honest runs over the STAR-invoking modules
#: DERIVED from this registry rather than a list typed beside it.
#: `--genomeLoad LoadAndKeep` reaches droplet's command line through `workflows/starsolo_args.py`,
#: which owns that argv whole; the memory instrument (`e2e.run_starsolo`) overrides it to
#: `NoSharedMemory`, because it has no load rule to create the segment and no handler to release one.
#: 2026.8.11 — `rule star_count` declares what it needs, which until now was NOTHING (#377): no
#: `mem_mb`, so a bulk run's largest single allocation was invisible to whatever packs the machine,
#: and no `--limitBAMsortRAM`, so the coordinate sort ran on STAR's default of `0` — "reuse the
#: genome allocation", a budget nobody chose that tracks the genome's size instead of the sample's
#: depth, and the one value STAR refuses outright once a run shares a copy of the index (refused
#: before the genome directory is read, so it would be every sample on the first attempt).
#: It gains the arrangement `starsolo_count` already has — a request linear in `attempt`, its own
#: retry count, and a cap derived from the memory THAT attempt was granted, declared as a `resources:`
#: entry because a `params:` one is expanded on attempt 1 and reused by every retry. The count is its
#: own (`BULK_RETRIES`) rather than STARsolo's, so one workflow's headroom is not a function of the
#: other's, and the reasoning differs too: STARsolo escalates against `readInfo`, which grows with
#: every input read, while bulk counts genes, holds no such array, and escalates for DEPTH alone.
#: **WHICH OUTPUT MOVES.** None. `--limitBAMsortRAM` is a cap and never an allocation — STAR reports
#: the memory the sort needed and refuses above the cap, so a sample that fits today produces the
#: same counts and the same BAM. Only `map/star` moves; the other three modules are untouched. The
#: bump is owed by the `.smk` edit, and a `run_id` is its whole cost.
#: 2026.8.10 — `rule starsolo_count` asks the chemistry which trimmer to run instead of telling it
#: (#355). `--clipAdapterType` was a module literal, `CellRanger4` for all eleven starsolo
#: chemistries; it is a required KB parse key now, and `--clip5pAdapterSeq` joins it as the optional
#: five-prime override one chemistry declares.
#: **WHICH OUTPUT MOVES.** The five three-prime 10x chemistries (`10x-3p-gex-v2`, `-v3`, `-v3.1`,
#: `10x-gemx-3p-v4`, `10x-multiome-gex`) declare `CellRanger4` and emit a BYTE-IDENTICAL command
#: line — verified by diffing the rendered `starsolo_count` shell block across the change, not
#: assumed from the value. Their counts are unchanged and stay comparable to a published CellRanger
#: matrix, which is the whole reason the value is what it is. The other six change: the two
#: five-prime 10x entries and the three BD Rhapsody ones take `Hamming` and stop having a
#: three-prime kit's TSO clipped off a read their own vendor pipeline never trims, and SPLiT-seq
#: keeps the clip with its OWN 30 nt TSO in place of the 10x one it differs from at two positions.
#: Both flags render from ONE helper rather than two tokens, because STAR makes them one decision:
#: the override REPLACES `CellRanger4`'s hardcoded sequence instead of adding to it, and
#: `CellRanger4` is the only mode where a five-prime override is legal at all. Fusing them is also
#: what makes the three-prime rendering byte-identical rather than merely argv-equivalent — a second
#: token would have left an empty continuation on every chemistry that declares no override.
#: The trimmer belongs to the entry and not to this module because its right value MOVES from one
#: chemistry to the next — the ownership test stated on `Backend` in `kb/schema.py`, resting on the
#: per-vendor review in `docs/research/starsolo-read-preprocessing-per-family.md`.
#: **AND THIS MODULE CAN CLIP A READ-THROUGH NOW**, so `--clip3pAdapterSeq` renders here too, at
#: arity ONE. `--readFilesIn` hands the rule two files, but solo peels the barcode read off and only
#: the cDNA read is a mate, so a second value — `-`, STAR's own per-mate no-clip sentinel, included —
#: is a hard FATAL at parameter init, measured under both soloTypes against the pinned 2.7.11b.
#: `map/star-umi` renders the same flag at its cell's mate count, so the two modules land on opposite
#: answers from one rule. It also puts the clip out of the barcode read's reach STRUCTURALLY: there is
#: no mate to aim at it, hence no sentinel to get wrong and no way to trim a CB or a UMI.
#: `--clip3pAdapterMMp` is NOT restated here, unlike there — STAR's default is a single 0.1, which
#: already matches this arity, and a flag that reads as a decision and is the default is worse than
#: silence. This is what lets the three BD Rhapsody entries declare BD's own 38-base poly-A and both
#: five-prime 10x entries the 5' TSO's reverse complement, and it is why the params gate stops
#: refusing a `read_through` on this pipeline.
#: **EVERY DATASET RE-KEYS, not only the eleven chemistries this module serves** — a Smart-seq3 plate
#: on `map/star-umi` and a bulk deposit on `map/star` each get a new pipeline directory though nothing
#: about their processing moved. Why a stamp does that, and the coupling that owns it (#361): the
#: `KB_VERSION` note in `kb/__init__.py` for the release that moved eleven chemistries.
#: 2026.8.9 — `rule star_umi_map` clips the chemistry's read-through, per mate (#356). The KB states
#: the adapter once and this module renders the flag, so `map/star-umi` becomes the first pipeline
#: that honours a `read_through`; a chemistry declaring one whose pipeline does not is refused at
#: compose rather than composed unclipped, because an adapter nothing removes still costs the reads
#: it sat inside and now nothing says so.
#: **Per MATE, and STAR counts.** Measured against 2.7.11b at parameter init: `--clip3pAdapterSeq`
#: takes one value per mate, and `--clip3pAdapterMMp` must match its arity or the run is refused
#: outright — so both are rendered from `mate_count`, the fact `--readFilesType` was already derived
#: from, and `read_files_type` now reads it too rather than asking the layout a second time. This
#: module's mate count is per SAMPLE, so a flag rendered once for the run would be fatal on every
#: cell of the other kind; one plate legally mixes both. `--clip3pAdapterMMp 0.1` is STAR's own
#: default restated at the arity the paired form demands, and a module literal because it varies with
#: nothing.
#: The clip rides the ALIGNER and never the extractor, which is what puts it after UMI extraction by
#: construction: the tag and UMI are the first bases of the tagged read, so anything trimming that
#: read earlier destroys the UMI. The uBAM is the edge between the two rules and the ordering is not
#: a convention anybody has to remember. Every record it reaches is cDNA for a reason that is also
#: not this module's: compose places the mate by ROLE and the params gate re-checks the placement.
#: **What it buys, measured** (#358, four published GSE207085 cells, one flag the only difference):
#: `unmapped: too short` 54.43% -> 22.75% and uniquely mapped 42.39% -> 63.59%, with the mismatch rate
#: flat, so the recovered alignments are not junk waved through. About seven in ten freed reads become
#: uniquely mapped; the rest were almost entirely adapter and become stubs too short to seed, which
#: clipping reveals rather than causes. Same run settles the one thing a dry run cannot: the clip DOES
#: apply to SAM/BAM input read through `--readFilesCommand`, which is the only form this module uses.
#: Full tables: `docs/research/smartseq3-tn5-read-through.md`.
#: 2026.8.8 — `rule umi_count`'s docstring stops counting the matrices it writes (#338, from #333).
#: **Prose only: no rule, no param, no output changes, and a pipeline recompiled under this version
#: produces byte-identical config and units.** The bump is unavoidable anyway — `run_id =
#: H(dataset | processing | kb | workflow)` and editing a shipped `.smk` at all moves the workflow
#: axis, so a comment costs one round of `run_id` invalidation exactly like a rule would. That price
#: is why the sentence was left wrong when 2026.8.7's release made it wrong, and why the fix is to
#: REMOVE the fact rather than correct it: the count lived in three prose spots and a table, one spot
#: drifted to four matrices for a release that shipped five, and correcting it would have bought the
#: same invalidation for the same exposure again on the next layer. The table on
#: `workflows.umite.count` now says how many there are and nothing else does, so the next matrix
#: added cannot make a shipped file lie and no `WORKFLOW_VERSION` bump is owed for prose again.
#: 2026.8.7 — `rule umi_extract` renders the **Units table** and the wildcard, and NO file (#327,
#: ADR-0036). `ordered_fastqs` returns a list, so `--r1 {input.tagged}` expanded a cell's two files
#: after a one-value option and died at job execution with `exit 2, Got unexpected extra argument(s)`
#: — on the 20 of 190 well-labelled plate deposits that are not strictly 1:1, and invisible to the
#: wiring gate, which FORMATS a `shell:` block while planning and never runs one. The verb resolves
#: its own list from the same table through the same helper the rule's `input:` is built from, so
#: this is one derivation used twice rather than two, and the FASTQs stay declared inputs because
#: what snakemake stages is still the rule's to state. `mate_arg` is gone; `units.tsv` becomes a
#: declared input; `read_files_type`'s `SAM SE`/`SAM PE` derivation is untouched and still reads the
#: same per-sample mate list. Only this module's command line moves — the other three are untouched
#: — but the bump is owed by any `.smk` edit, and a `run_id` is its whole cost.
#: 2026.8.6 — a FOURTH module, `map/star-umi`: the pre-demultiplexed, one-cell-one-file pipeline
#: (#291). Per cell `extract -> STAR -> one coordinate-sorted BAM`, then ONCE `count(N BAMs) -> one
#: combined .h5ad`. Purely additive — the three shipped modules' rules, params and command lines are
#: untouched — so the bump is owed for the module's arrival and not for a change to anything already
#: composed. Four things arrive with it. (a) A fourth `read_layout_kind`, `umi_tagged`, emitting
#: `{umi_cdna, cdna}` chosen by ROLE: `barcoded` refuses correctly (no CB element exists anywhere in
#: the layout), and `mates` is the DANGEROUS one — it picks by ORDER, and these two mates are not
#: symmetric, so a layout listing the plain mate first hands the untagged read to the extractor for
#: 0% UMI yield AT EXIT 0, which is precisely the silent fall-through `read_layout_kind` was created
#: to kill reappearing in the kind that looks like the safe default. The new kind tolerates one read
#: structurally, so single-end needs no fifth kind later. (b) A fan-in step DECLARED on the module
#: (`fan_in_artifact`), naming the dataset-scoped deliverable; absent on the other three. Left
#: implicit, `pipeline.sample_dir()` and the report assume per-sample forever and the next
#: cross-sample module discovers that the way the last three discovered `read_layout_kind`,
#: `param_block` and the stats registry. (c) The aligner-param block literal gains `umi`, without
#: which `param_block` raises and compose dies on this module. (d) `parse_keys` stay EMPTY and the
#: whole extraction geometry is DERIVED into one key: the anchor and its offset, the UMI's offset and
#: length, the trailing motif and the cDNA start are all read off the element coordinates, exactly as
#: `soloAdapterSequence` moved out of the declarable set one chemistry earlier. `aligner` stays
#: derived from the id's tail, so the tail has to be a true statement on its own — and it is: STAR
#: aligns. The id names the MECHANISM, which is what makes SMART-seq2's inability to use it legible
#: from the id alone (also plate-based, also one cell per file, no UMI at all).
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
#: decides it, is ADR-0011.
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
WORKFLOW_VERSION = "2026.8.17"

_MODULE_DIR = Path(__file__).parent

#: liulab-runtime's published image. **A reference to their artifact, never a definition of one**:
#: we name a tag they build and push, and this repo contains no conda YAML and no Dockerfile.
#: `align-rna` is where the STAR that RUNS A DATASET comes from, and no dependency table here can
#: reach a rule. A test-only pixi environment does carry a STAR the `external` tests exec; it ships
#: in no wheel, joins no solve group, and is invisible from here.
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


#: The config blocks that carry an aligner's params. One per module, and :meth:`param_block` refuses a
#: module that reads none or several. Also the parameter names an argv renderer binds its block to,
#: which is what lets :func:`argv_keys_read_by` find the reads without being told where to look.
_PARAM_BLOCKS: frozenset[str] = frozenset({"solo", "bulk", "chromap", "umi"})


def argv_keys_read_by(source: Path) -> frozenset[str]:
    """The dotted config keys an argv renderer reads, **walked out of its AST**.

    The counterpart to :func:`keys_read_by` for the half of a module's config reads that no longer
    live in its Snakefile. `starsolo.smk` used to render STAR's whole command line itself, so
    scanning that one file for ``SOLO["..."]`` recovered every key the composer owes; the geometry
    and clip closures now live in `workflows.starsolo_args`, where a Snakefile scanner cannot see
    them (#348).

    **An AST rather than a second regex, and it is strictly more precise than the one it joins.** A
    renderer is importable Python, so every branch is visible at once — the scan does not have to
    take one. That matters here specifically: :func:`~seqforge.workflows.starsolo_args.cb_umi_geometry`
    reads four keys on its simple arm and two on its Complex arm, and a reader that followed
    execution would see whichever the sample happened to be.

    A **subscript** is a key the composer owes; a ``.get`` is one only the chemistry that has it
    emits, and is deliberately not returned — the same line `keys_read_by` draws, drawn exactly here
    rather than by pattern. The block is found by parameter name (:data:`_PARAM_BLOCKS`), so a
    renderer states which block it renders by taking it as an argument, not by declaring it twice.
    """
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if not isinstance(node, ast.Subscript):
            continue
        block, key = node.value, node.slice
        if (
            isinstance(block, ast.Name)
            and block.id in _PARAM_BLOCKS
            and isinstance(key, ast.Constant)
            and isinstance(key.value, str)
        ):
            keys.add(f"{block.id}.{key.value}")
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
        # Which read-preprocessing STAR runs before alignment, by the SAME test as the key above and
        # with the same history: a module literal (`CellRanger4`, always) until #355, because "chosen
        # for CellRanger parity" was read as evidence about ownership. It is not — it is a reason to
        # pick a value, and the correct value moves between chemistries — the argument is stated on
        # `Backend` in `kb/schema.py` and the per-vendor evidence in
        # `docs/research/starsolo-read-preprocessing-per-family.md`. REQUIRED of all eleven rather
        # than optional-with-a-default, deliberately: whichever group stayed silent would be defined
        # by silence, and a new spec would join it by accident.
        "clipAdapterType",
        # The five-prime override, and only `CellRanger4` takes one — it REPLACES that mode's
        # hardcoded 10x TSO rather than adding to it, which is what lets a chemistry with its own TSO
        # keep the clip and fix the sequence. Declared by SPLiT-seq alone today. A parse key beside
        # its trimmer rather than a top-level term, because a top-level term is what a fact TWO
        # pipelines consume earns (`read_through`); a five-prime override is starsolo-only until a
        # second module can honour one, and promoting it early would be a term with one reader.
        "clip5pAdapterSeq",
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
#: - ``umi_tagged``    — ``{umi_cdna, cdna}``, chosen by ROLE (a pre-demultiplexed plate assay: one
#:   mate opens with a tag + UMI + motif and the other is plain cDNA, with NO barcode read at all).
#:
#: A typed, visible choice rather than the old ``spec.backend.module == "map/starsolo"`` string
#: compare, in which every module that was not starsolo silently fell into the bulk mate1/mate2
#: branch and emitted a wrong command line. A third module must pick a kind, or add one.
#:
#: **``umi_tagged`` is not cosmetic, and ``mates`` is the reason.** ``barcoded`` refuses a plate
#: correctly — no ``CB`` element exists anywhere in the layout, so the lookup returns nothing — but
#: ``mates`` accepts it and picks by ORDER, and a plate's two mates are *not* symmetric: one carries
#: the tag. A layout listing the plain mate first hands the untagged read to the extractor, nothing
#: is tagged, and the run ends in an empty matrix at **exit 0**. That is exactly the silent
#: fall-through this field exists to kill, reappearing in the kind that looks like the safe default.
#: The kind also **tolerates one read** structurally — no minimum-of-two assert to undo later — so a
#: single-end plate needs a wider extractor rather than a fifth kind. Named for the READ property
#: rather than for the sample axis: ``plate``/``cell_per_file`` would be a second place to say what
#: the KB's ``identity.sample_is_cell`` already says, and ``tagged``/``untagged`` is the per-read
#: vocabulary the counting design uses *inside* one read.
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
ReadLayoutKind = Literal["barcoded", "mates", "atac_barcoded", "umi_tagged"]


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
    #: The Python module that renders this pipeline's aligner argv, where it has one. Scanned for
    #: config reads exactly as the Snakefile is (:func:`argv_keys_read_by`), because a module whose
    #: command line moved out of its ``shell:`` block still owes the composer the same keys — the
    #: reads moved, the contract did not. ``None`` for a module that renders its own argv inline.
    argv_source: Path | None = None
    #: The parse-param namespace this pipeline's KB backends may declare (byte-decided knobs). Empty for
    #: a bulk pipeline that takes no parse params. Per pipeline, so a chromap backend declares chromap's
    #: parse knobs and a starsolo backend declares ``solo*`` — each gated against its own namespace.
    parse_keys: frozenset[str] = frozenset()
    #: Which assay modalities this pipeline serves. The adapter refuses to bind a spec whose modality is
    #: not here, so an RNA chemistry can never be composed against an ATAC-only pipeline (or vice versa).
    serves_modalities: frozenset[str] = frozenset({"rna"})
    #: The DATASET-scoped deliverable this pipeline fans in to, if it has one — one filename under the
    #: results directory, produced once for the whole deposit rather than once per sample. ``None``
    #: for a pipeline that is per-sample end to end, which is the three shipped ones and the default.
    #:
    #: **Declared rather than inferred, and minimal on purpose.** A plate assay counts every cell in
    #: one job and writes one combined object, so "this module produces something the sample axis
    #: does not reach" is a fact about the module — and this repo has already paid three times for
    #: the implicit version of exactly that shape: ``read_layout_kind`` exists because a module-name
    #: compare made every non-starsolo module silently mean bulk, ``param_block`` raises rather than
    #: guessing which config block a module reads, and the stats registry names the modules that
    #: report nothing so a fourth aligner cannot be silently absent from every page. Left implicit
    #: here, the per-sample assumption baked into a pipeline directory's layout and its report holds
    #: forever and the next cross-sample module discovers it the way those three were discovered.
    #:
    #: It NAMES the artifact and stops there — not a scope algebra, and not the rule's resource class
    #: (which the module's own arithmetic owns, because only the module knows its rule graph).
    fan_in_artifact: str | None = None
    #: This pipeline's chimera-aware twin: the module id to compose instead when the recipe names a
    #: **chimera** — one reference built from several component assemblies. ``None`` for a pipeline
    #: that has no such twin, which is every shipped one, and a recipe naming a chimera against one of
    #: those is a compose refusal rather than a run that merges two organisms into a single count
    #: table with nothing in it saying which reads were whose.
    #:
    #: **Declared rather than inferred, by the argument ``fan_in_artifact`` makes directly above and
    #: by one more that is specific to this fact.** Whether a pipeline can keep the components apart
    #: downstream of the aligner is a property of its rule graph, and every way of inferring it reads
    #: something that is not: a module-id compare is ``read_layout_kind``'s dead ancestor, in which
    #: everything that was not starsolo silently meant bulk; a lookup in the shipped assembly table is
    #: worse *here* than that pattern is anywhere else, because nothing folds the `liulab-genome` pin
    #: into ``run_id`` — a table-derived answer would let one recipe compile to a DIFFERENT module at
    #: the SAME ``run_id``, into the directory the first compile's alignments are already in.
    #:
    #: It NAMES the twin and stops there. Which components a chimera has is the recipe's statement
    #: (`compose.chimera`), and what the twin then does with them is the twin's own contract.
    chimeric_variant: str | None = None

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

        **Two sources, because the reads live in two files now.** A module whose argv renderer moved
        into Python (`argv_source`) is scanned there too, and by AST rather than by regex — see
        :func:`argv_keys_read_by`. Deriving from wherever the reads actually are is the same rule as
        before; what changed is that "wherever" stopped being one file.
        """
        keys = keys_read_by(self.snakefile)
        if self.argv_source is not None:
            keys |= argv_keys_read_by(self.argv_source)
        return tuple(sorted(keys))

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
        blocks = sorted({k.split(".")[0] for k in self.required_config} & _PARAM_BLOCKS)
        if len(blocks) != 1:
            raise ValueError(
                f"{self.name} reads {blocks or 'no'} aligner-param block(s) in its config; expected "
                f"exactly one of solo/bulk/chromap/umi. A module whose contract is unreadable must "
                f"not be guessed at — add the block it reads, or teach `param_block` the new shape."
            )
        return blocks[0]


#: What ``map/star-umi`` fans in to: ONE ``.h5ad`` over every cell of the deposit, under the results
#: directory and named for nothing in particular — there is no sample to name it after, which is the
#: whole point of the artifact.
#:
#: Here rather than in the counter, because the counter does not choose it: ``write_umi_counts``
#: writes wherever it is pointed, so the two places that decide this name are the registry entry
#: below (which DECLARES the deliverable) and the rule that produces it. Both read this constant, so
#: the declaration and the rule cannot come apart — the same one-owner-two-readers discipline the
#: QC-suffix constants get, applied to the one artifact that has no ``{sample}`` in it.
PLATE_H5AD = "combined.h5ad"

#: What ``map/star-umi-chimera`` fans in to: one ``.h5ad`` **per Component** over every cell, the same
#: artifact one arity out. The ``{component}`` placeholder is what makes it that: inserted into the
#: rule's f-string it survives as a snakemake wildcard, so the counting rule fans in once per
#: Component and the declaration below and the rule that produces it still read one constant. The
#: precedent for a placeholder in a declared artifact name is the stats registry's ``{sample}``
#: templates; the reason it is here rather than a second field is that a fan-in artifact's NAME has
#: exactly one owner and an arity is part of a name.
#:
#: Two objects and not one hstacked one, deliberately: a merged matrix would make the two organisms
#: tellable apart only by a gene-name prefix, forever, downstream of everything.
PLATE_COMPONENT_H5AD = "combined.{component}.h5ad"

MODULES: dict[str, WorkflowModule] = {
    "map/starsolo": WorkflowModule(
        name="map/starsolo",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "starsolo.smk",
        read_layout_kind="barcoded",
        argv_source=Path(__file__).parent / "starsolo_args.py",
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
    # The pre-demultiplexed, one-cell-one-file pipeline. `align-rna` is forced rather than chosen —
    # STAR is what needs the image, and the extractor and the counter shell out to nothing at all.
    # `parse_keys` is EMPTY and stays empty: every knob the extractor needs is already in the element
    # model, so the whole geometry is DERIVED into one config key rather than declared six times over
    # (see `compose.params.DERIVED_PARAM_KEYS`). A backend that declares one is refused at load.
    "map/star-umi": WorkflowModule(
        name="map/star-umi",
        version=WORKFLOW_VERSION,
        env="align-rna",
        snakefile=_MODULE_DIR / "map" / "star-umi.smk",
        read_layout_kind="umi_tagged",
        fan_in_artifact=PLATE_H5AD,
        # The one module that can keep a chimera's Components apart downstream of the aligner, so it
        # is the one module that declares a twin. The other three still declare nothing and a recipe
        # naming a chimera against any of them is a compose refusal — better a refusal than a count
        # table merging two organisms with nothing in it saying which reads were whose.
        chimeric_variant="map/star-umi-chimera",
    ),
    # The chimera-aware twin, reachable ONLY through the declaration above: no KB backend may name it
    # (`Backend._not_a_chimeric_variant` refuses one at load), so nothing bypasses the single dispatch
    # rule in `compose.plan`. Its `parse_keys` are empty for the base's reason and one more — with no
    # backend able to name it, there is no declarer left for a namespace to serve.
    #
    # Everything else it declares is the base's, and must be: same env because it runs the same STAR,
    # same read layout because it maps the same plate, same modalities. What differs is the fan-in
    # artifact's ARITY — one object per Component instead of one for the deposit — which is why the
    # name carries a `{component}` and why `rule all` demands each one by name.
    "map/star-umi-chimera": WorkflowModule(
        name="map/star-umi-chimera",
        version=WORKFLOW_VERSION,
        env="align-rna",
        # The basename is FORCED rather than chosen: `CompiledPipeline.module` inverts a run
        # directory's module identity out of the copied `.smk`'s basename, so a file named anything
        # else would compile and then report as no module at all.
        snakefile=_MODULE_DIR / "map" / "star-umi-chimera.smk",
        read_layout_kind="umi_tagged",
        fan_in_artifact=PLATE_COMPONENT_H5AD,
    ),
}

#: The module ids that are a twin of some other module — **derived from the registry above**, so
#: there is no second list for a new twin to be missing from. A KB backend naming one of these is
#: refused: the twin is selected by ``compose.plan`` swapping it in for its base under a chimeric
#: assembly, and a spec naming it directly would reach the same module by a route that never asked
#: whether the recipe's reference is a chimera at all.
CHIMERIC_VARIANTS: frozenset[str] = frozenset(
    m.chimeric_variant for m in MODULES.values() if m.chimeric_variant is not None
)


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
    "CHIMERIC_VARIANTS",
    "WORKFLOW_VERSION",
    "PLATE_COMPONENT_H5AD",
    "PLATE_H5AD",
    "RUNTIME_IMAGE",
    "ReadLayoutKind",
    "WorkflowModule",
    "MODULES",
    "argv_keys_read_by",
    "container_uri",
    "get_module",
    "keys_read_by",
    "list_modules",
    "parse_keys_for",
    "resolve_pipeline",
]
