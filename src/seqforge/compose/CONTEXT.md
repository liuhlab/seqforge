# Compose

Compiling one **Manifest** under one **Recipe** into a directory the user can submit — a Snakefile,
its config, and the table that says where every FASTQ is. This context also holds the words for what
a finished pipeline reports back.

Words every context shares — **Evidenced**, **Basis**, **Observed**, **Conflict** and the rest — are
defined once in the repo-root `CONTEXT-MAP.md`.

## Language

### Compiling

**Units table**:
The table `compose` writes beside a **Compiled pipeline**'s config: one row per FASTQ, naming its
**Sample**, its **Run**, its **Lane**, and which layout read it is. It is the only statement of
where a run's files are and what order they arrive in, so every consumer reads placement from here
rather than deriving it a second time.
_Avoid_: **sample sheet** — bcl2fastq's `SampleSheet.csv` maps indices to samples *upstream* of us,
a different mapping one layer up; also manifest and `units.tsv` (a filename)

**Recipe**:
`processing.yaml` — what to DO with a **Manifest**: genome, aligner, what to count, environment,
resource hints. Plural per dataset and sparse; empty is legal, and unpinned it is a template.
_Avoid_: config, settings, pipeline, params (`backend.params` is the disjoint parse half);
`ProcessingManifest` is the class, "recipe" is the word

**Memory figure**:
`resources.mem_gb` in a **Recipe**: what one mapping job asks the scheduler for, covering everything
that job needs — index residency, working set, and a BAM sort that scales with the sample — as though
it were the only job on the machine. Self-sufficient rather than incremental, which is why it cannot
double as a limit on how many jobs a machine admits.
_Avoid_: memory cap (`--limitBAMsortRAM` is the cap, and is three quarters of this), RAM budget, node
memory; and never a cost *additional* to a **Shared genome copy**

**Dataset hash**:
The sha256 over exactly the **Manifest**'s `library` and `experiment` sections. Invariant under
every change of intent — that invariance is what lets one manifest compile many ways.
_Avoid_: manifest hash (the provenance block carries the hash and sits outside it), checksum,
dataset id

**`run_id`**:
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)` — the identity of one *pairing*, computed at compile time
and stored inside neither input. It names the pipeline directory, so two recipes over one dataset
cannot overwrite each other. Its `kb` term is a content hash of the one spec that decided the
config, not a repository-wide version, so a release that leaves this chemistry's processing alone
leaves this pairing's identity alone.
_Avoid_: build id, job id, provenance id; and never for `RunResolution.run_id`, which is a
filename-derived **Run** key

**Compiled pipeline**:
One `(manifest, recipe)` pairing made runnable — the directory `compose` writes (the Snakefile, its
config, the **Units table**, a copy of the **Workflow module**, and the exclusion record when a
sample was kept out), and the execution of that Snakefile. One word for both, because the directory
is where the execution's outputs land: what `compose` wrote is a function of the `run_id` naming it,
and what the execution wrote is not.
_Avoid_: **Run**, which is one *sequencing* run, and `run_id`, which names the pairing rather than
its execution; also build, job, workflow run

**Shared genome copy**:
The one aligner index a **Compiled pipeline** loads into shared memory at the start of a run and
every mapping job attaches to instead of loading its own — the reason a machine holds one index and
not one per concurrent job. It is per *machine*, and collapses to one copy per run only because a
run never spans machines.
_Avoid_: shared memory, segment, cached index — each names the mechanism rather than the thing; also
genome index, which is the on-disk directory `liulab-genome` owns and is what gets loaded

**Chimera**:
One `liulab-genome` reference built from several **Component** assemblies, whose chromosome names
each carry a component suffix at a separator that reference recorded. A **Recipe** states one by
naming it and nothing else — `compose` reads the *name* to select the chimera-aware **Workflow
module**, and every fact about what was actually built comes from the completion record at run time.
_Avoid_: combined/merged/hybrid genome, multi-species reference, co-mapping index; and never
*chimeric read*, which is one alignment split across loci and an unrelated thing

**Component**:
One assembly a **Chimera** was built from, and the axis every per-organism artifact is keyed by — one
BAM, one matrix, one kept count per component. Which annotation its matrix is counted against is
what that component *contributed to the merge*, read off the Chimera's record rather than typed,
because the merged annotation's name does not record who fed it.
_Avoid_: species and organism (a component is an assembly, and two of them can be one species);
host/contaminant/spike-in, which name a role no stage here privileges; also the architecture sense of
"component", which this project does not use at all — see the two-vocabularies note in the map

**Workflow module**:
A hand-written, versioned Snakemake module under `workflows/`. `compose` selects one and copies it
beside the emitted `Snakefile`, so a run directory reproduces after it is moved; nothing generates
rule source. One may declare a chimera-aware twin of itself, which is the only way that twin is ever
selected.
_Avoid_: template, generated rules, pipeline code, snippet

**Workspace**:
The user's project root, and the `seqforge/` state directory under it. No leading dot, because it
holds the manifest and the Snakefile — it is output, not cache; only `seqforge/cache/` is safe to
delete.
_Avoid_: `.seqforge`, output dir, cache dir, build dir

### After the run

**Metric**:
One number a finished **Compiled pipeline** wrote, with the words a human reads it by, the stage it
speaks about, and a **level** — `ok`/`warn`/`bad`, or `none` where no bar is defensible, which is a
verdict and not a missing value. The module that wrote the artifact decides the level.
_Avoid_: stat, QC number, score, grade; and never an **Observation**, which is read from the bytes
before anything ran

**Alert**:
Post-run evidence contradicting a decision already made and already hashed — a threshold comparison
over the **Metric**s a finished **Compiled pipeline** wrote, naming the decision it implicates and
the value that decision currently carries. The compiler's one backward edge, and advisory by
construction.
_Avoid_: **Conflict**, **Warning**, **Blocker**, **Question** — all four are compile-time and
carried by an exit code, and an alert is none of them arriving late; also issue, diagnostic, QC
failure

**Counting grid**:
The two axes a plate assay's matrices are crossed from: the **counting unit** (a UMI — a
deduplicated molecule — or a read, which cannot be deduplicated) against the **feature region**
(exon, intron, or the two **combined**). Six cells, five of them materialised as matrices, because
a matrix earns its place by being **non-derivable**, and the combined read figure is exactly the sum
of the other two.
_Avoid_: `inex` (zUMIs' spelling) and `U`/`UE`/`UI`/`RE`/`RI` (the reference tool's) — both need the
tool read first; also "spliced/unspliced", which is Velocyto's question about a **Read**

**Fan-in artifact**:
The deliverable a **Compiled pipeline** produces **once** for the whole **Deposit** rather than once
per **Sample** — one file carrying one row per sample, declared by a **Workflow module** as
`fan_in_artifact`. Dataset-scoped as a *file* and sample-scoped as *data*: it has no sample in its
path, so nothing addressed per sample can find it, while everything inside it still belongs to one
sample.
_Avoid_: merged output, aggregate, summary (all three read as a *derived* second copy — a fan-in
artifact is the primary result); combined file; and never for the **Manifest**

**Absent**:
Why a benchmark case produced no grade: the corpus does not hold its package. A standing fact about
the corpus, published rather than hidden, and the `skip_kind` that never poisons a rate.
_Avoid_: skipped, missing, failed — each names both this and **unavailable** (the package exists and
this run could not reach it, which is transient), and so distinguishes neither
