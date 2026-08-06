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

**Dataset hash**:
The sha256 over exactly the **Manifest**'s `library` and `experiment` sections. Invariant under
every change of intent — that invariance is what lets one manifest compile many ways.
_Avoid_: manifest hash (the provenance block carries the hash and sits outside it), checksum,
dataset id

**`run_id`**:
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)` — the identity of one *pairing*, computed at compile time
and stored inside neither input. It names the pipeline directory, so two recipes over one dataset
cannot overwrite each other.
_Avoid_: build id, job id, provenance id; and never for `RunResolution.run_id`, which is a
filename-derived **Run** key

**Compiled pipeline**:
One `(manifest, recipe)` pairing made runnable — the directory `compose` writes (the Snakefile, its
config, the **Units table**, and a copy of the **Workflow module**), and the execution of that
Snakefile. One word for both, because the directory is where the execution's outputs land.
_Avoid_: **Run**, which is one *sequencing* run, and `run_id`, which names the pairing rather than
its execution; also build, job, workflow run

**Workflow module**:
A hand-written, versioned Snakemake module under `workflows/`. `compose` selects one and copies it
beside the emitted `Snakefile`, so a run directory reproduces after it is moved; nothing generates
rule source.
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
