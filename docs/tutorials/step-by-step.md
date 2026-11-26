# Step-by-step: compiling a real dataset

This is the detailed walkthrough — the pipeline one verb at a time. If you just want to compile a
dataset, [driving seqforge with Claude](with-claude.md) is the shorter path; come here when you want
to see, and inspect, each stage.

This is the worked example, start to finish, on the dataset seqforge was built against:
**[PRJNA1027859](https://www.ncbi.nlm.nih.gov/bioproject/PRJNA1027859)** — six single-nucleus RNA-seq
runs of adult *C. elegans* neurons, wild-type versus *daf-2* mutants, from
[Cell Genomics 2024](https://doi.org/10.1016/j.xgen.2024.100720).

You will end with a Snakefile you can submit. **seqforge does not submit it** — no Slurm profile, no
`sbatch`, no executor. Its output is artifacts, and the last one runs.

Everything below is real output from a real run. Where a command is slow or needs 220 GB of FASTQ,
that is said.

---

## 0. What you have

Two things, and one of them is optional:

```text
data/
  SRX24283130/SRR28716558_1.fastq.gz    # 12 files, one directory per experiment,
  SRX24283130/SRR28716558_2.fastq.gz    # which is how `fasterq-dump` wrote them
  ...
info/
  paper.pdf                             # optional
```

Plus an accession, `PRJNA1027859`, which is also optional — plenty of data never had one. Each of
the three inputs makes the answer richer; none of them is required, and none of them is trusted.

## The short version: one pass

If you just want the manifest and the Snakefile, `seqforge run` does the whole thing and prints one
summary:

```bash
pixi run -- seqforge run data/*/*.fastq.gz \
    --accession PRJNA1027859 --doc info/paper.pdf \
    --assembly ce11 --annotation WS298 --fastq-dir data
```

It chains every stage below — records, the paper, the manifest, the recipe, the Snakefile — stops at
the first refusal, and writes everything under `seqforge/`. The genome is the one argument with no
default (a wrong one aligns cleanly and exits 0, so it will not guess). Reading the paper uses a
language model through the lab's model gateway: open a tunnel to it and set `LABLLM_GATEWAY` to its
local address ([Reach the lab gateway](https://liuhlab.github.io/liulab-llms/reach-the-gateway/)).
It spends at most a dollar unless `--ceiling-usd` says otherwise. Add `--no-llm` to skip prose and
stay fully deterministic.

The rest of this page walks the same pipeline one verb at a time — which is what you want when a step
needs inspecting, and what `run` is doing for you when it does not.

## 1. Ask the bytes what this is

```bash
pixi run -- seqforge resolve score data/SRX24283130/*.fastq.gz
```

This reads the **first 2 000 reads** of each file and never more (it is bounded by `--max-reads`
*and* by a 256 MB decompressed cap; a code path that could stream a whole multi-GB FASTQ is a bug
here, not a slow path). Two thousand reads is plenty — the chemistry is the same whether you look at
one thousand or two hundred thousand — and you can raise `--max-reads` to sample more of a full-size
file. It scores every technology in the knowledge base against what it saw and prints the ranked
candidates, the role assigned to each file, and the rung that settled it.

Note what it does **not** do: read the filenames for meaning. `_1` and `_2` are `fasterq-dump`'s dump
order and say nothing about which read is the barcode. Roles come from bytes.

Add `--explain` to see the evidence matrix — one row per read role, one column per file — when you
want to know *why*.

## 2. Ask the archive what it declares

```bash
pixi run -- seqforge io records PRJNA1027859
```

```json
{
  "records": "seqforge/records/PRJNA1027859.json",
  "query": "PRJNA1027859",
  "source": "ncbi-sra",
  "n": { "project": 1, "sample": 6, "experiment": 6, "run": 6 }
}
```

Four levels, joined by the archive's own accessions:

```text
BioProject PRJNA1027859      title, abstract, centre, data type
  └─ BioSample SAMN40935621     strain=CQ758, tissue=Neurons, sex=hermaphrodite, dev_stage=Adult Day 1
       └─ Experiment SRX24283135   instrument, library strategy, and the protocol paragraph
            └─ Run SRR28716553        the alias "Rep3 daf2 reads", and your files
```

This verb is a **transcriber**. It reports what the submitter typed and stops; it decides nothing.

The run and sample records are what match each of your files to its sample, and the records declare
the organism and the project. What each sample *was* (its strain, tissue or treatment) is not part of
the compiled manifest: a study reading holds those (see [Study workflow](../study-workflow.md)).

## 3. Write down what the data IS

```bash
pixi run -- seqforge manifest fill data/*/*.fastq.gz --accession PRJNA1027859
```

Two resolvers run here, and they answer different questions.

`resolve score` reads the **bytes** and decides the library: which chemistry, which file is which
read. The metadata resolver reads the **records** and decides which sample each file is. Neither is
shown the other's input — the metadata resolver is handed file names and hashes, not the probe's
output, because the join is made by the names the records declare.

Both can refuse.

You get `seqforge/manifest.yaml`:

```yaml
experiment:
  organism:
    value: 6239
    basis: asserted
    evidence: [SAMN40935616, SAMN40935617, SAMN40935618,
               SAMN40935619, SAMN40935620, SAMN40935621]
    confidence: null
    rung: 0
  project:
    accession: PRJNA1027859
    title: A Single-Nucleus Atlas of Adult C. elegans Neurons Reveals GPCR and Insulin-signaling Profiles
    center: Princeton University
    data_type: raw sequence reads
  samples:
    - sample_id: SAMN40935616
      accession: SAMN40935616
      file_uris: [SRX24283130/SRR28716558_1.fastq.gz, SRX24283130/SRR28716558_2.fastq.gz]
    # ... five more
```

Two things worth reading closely.

**You did not pass `--organism`.** The record declares it, and the manifest cites the six BioSamples
it came from. A flag still wins if you pass one — a human typing a taxid is asserting it now, having
looked — but nothing is defaulted. There is no default taxid and there must not be: a wrong one
aligns cleanly against the wrong genome, exits 0, and nothing downstream ever asks again.

**`confidence: null` is the informative value.** It means nothing was judged. Copying the taxid out
of a BioSample record is a transcription — `basis: asserted` plus the record accessions in
`evidence` already says everything true about how we know it. A `1.0` there would invite the question
it cannot answer ("you are certain it is 6239?"). We are certain the *records declare* it, which is a
different claim and the one we make.

### If you have no accession

```bash
pixi run -- seqforge manifest fill reads/*.fastq.gz --organism 6239
```

That works, and it is the common case. Files are grouped into runs by name, and each run becomes a
sample. Exit 0, and a manifest that is just as true.

### If the accession does not match your files

That is a refusal, not a shrug:

```text
RECORD_JOIN_INCOMPLETE: PRJNA1027859 declares 6 run(s) (SRR28716553, ...), and 1 file(s) on disk
match none of them by run accession or by the original filenames the record declares: mystery_1.fastq.gz
```

Half-joining would leave that file with no sample while the manifest still read as though it
described everything.

## 4. Let the model read the prose (optional)

```bash
pixi run -- seqforge harvest extract info/paper.pdf --records seqforge/records/PRJNA1027859.json
pixi run -- seqforge manifest fill data/*/*.fastq.gz --accession PRJNA1027859 \
    --assertions seqforge/logs/assertions.json
```

This is the **only** stage that touches a language model. A model writes an answer only where code can
check it. Here that means two jobs: turn prose into claims that carry a quote, and settle an
ambiguity code has already flagged. Code decides whether each answer stands. Everything else in
seqforge is a verifier.

Every claim it returns carries a verbatim quote. Code — not the model — greps that quote back into
the normalized document, computes the offsets, and checks that the quote actually supports the value.
A claim that fails either check is discarded.

**What the model is asked.** From a paper: the chemistry, whether the input was cells or nuclei, the
organism and the accessions. `--records` renders each experiment record as its own document, and its
protocol paragraph is asked for the chemistry and nothing else. Sample and run records, and the study
abstract, are asked nothing. What each sample was is not the compiler's question; a study reading
answers it.

**A chemistry claim is a hint, never evidence.** It can break a tie the bytes cannot settle, and a
disagreement between what the paper claims and what the bytes show is a question for you, never
something code picks.

## 5. Write down what you want DONE with it

```bash
pixi run -- seqforge processing new seqforge/manifest.yaml \
    --assembly ce11 --annotation WS298 --out seqforge/processing.yaml
```

A dataset is immutable; what you do with it is a choice. So they are two files, and there are as many
processing manifests as you care to run. **If you find yourself editing `manifest.yaml` to change how
something is processed, stop** — that is the bug the split exists to prevent.

`manifest fill` takes no genome, deliberately. Choosing a reference is intent, not something you learn
by probing bytes.

You will not need `--quantify` here. The default counts all five STARsolo features in one pass — one
alignment, five counting rules — so this being single-*nucleus* data (full of unspliced pre-mRNA, and
therefore needing `GeneFull`) costs you no decision and no question. Where every answer is affordable,
produce every answer.

## 6. Compile

```bash
pixi run -- seqforge compose seqforge/manifest.yaml \
    --processing seqforge/processing.yaml --fastq-dir data
```

```json
{
  "snakefile_path": "seqforge/pipeline/default-d94c737eb677/Snakefile",
  "config_path": "seqforge/pipeline/default-d94c737eb677/config.yaml",
  "units_path": "seqforge/pipeline/default-d94c737eb677/units.tsv",
  "gate": {
    "params": { "status": "pass", "reason": [] },
    "wiring": { "status": "pass", "reason": [] },
    "e2e": { "status": "skip", "reason": ["the end-to-end toolchain is incomplete here: ['STAR'] absent"] }
  }
}
```

The directory is named for the recipe plus the first 12 characters of the run id. The run id is
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, so compiling this dataset a second way gives you a second
directory rather than silently overwriting the first — and the dataset's own hash does not move.

**That hash is one run's, and it will not be yours.** `workflow` in the expression is the compiler's
own version, so a release moves the directory name for the same dataset and the same recipe. Step 7
therefore takes the path from your own output, never from this page.

Each gate carries its own reason beside its verdict, because a verdict alone does not say what to do: a
failing wiring gate hands back what snakemake said, and a `skip` says what it was waiting for.

The gates:

- **params** — the semantic checks a dry run cannot make. Is `--soloUMIlen` 12 when the reads carry a
  12 bp UMI? Does `--readFilesIn` put the cDNA read where the cDNA read belongs? These are the bugs a
  config compiler actually produces, and they fail *silently*: STARsolo exits 0 and emits a matrix
  that merely looks like a thin dataset.
- **wiring** — `snakemake -n` over a throwaway replica. It proves the workflow plans jobs. A dry run
  that plans *nothing* also exits 0, so the gate looks rather than trusting the code.
- **e2e** — the real count-matrix run. Reported `skip`, never silently `pass`: it needs STAR and a
  genome, and a gate that says "pass" because it never ran would let green CI be mistaken for
  coverage.

## 7. Submit it

The directory to go into is the one `compose` just printed — its `snakefile_path`, minus the
`Snakefile` on the end:

```bash
cd seqforge/pipeline/<the-directory-compose-named>
snakemake --software-deployment-method apptainer
```

**This part is yours.** seqforge has no opinion about your scheduler, and it will not grow one.

`--software-deployment-method apptainer` is what makes the alignment rule run inside the pinned
`liulab-runtime` image. Without it snakemake ignores the `container:` directive entirely and STAR
comes from your `PATH`: the run still works, and it is no longer pinned.

**Run it on one machine.** The pipeline loads the genome index into shared memory once and every
mapping job attaches to that one copy, so several samples mapping at the same time cost one index
between them rather than one apiece — on a human genome that is the difference between ~31 GB and
~31 GB times however many jobs are running. Spreading the jobs across machines with a cluster
profile still satisfies every dependency in the workflow and silently gives each job its own copy,
because an edge guarantees order, never that two jobs share a machine. Scale comes from running many pipelines at
once, not from fanning one out. Give the run a whole machine and let `--cores` decide how many
samples map at a time.

The default target is `all`, which demands the matrices — one `<sample>.h5ad` per sample, plus a
`<sample>.velocyto.h5ad` — not a folder that might be empty.

The 111 MB barcode whitelist is not in that directory, and should not be. A rule builds it, STAR
reads it once, and snakemake deletes it.

## When the project isn't uniform

Every run of this dataset is the same 10x chemistry, so it compiled to **one** manifest and **one**
Snakefile. A study that mixes chemistries — 10x v2 *and* v3 libraries in one series, say — compiles to
one of each **per assay**: `seqforge/<assay>/manifest.yaml` and its own Snakefile, one per chemistry,
with the split decided from the bytes rather than by you. Two project-level files then sit at the top,
`sample_metadata.tsv` (every sample across every assay, one row each) and `project.yaml` (the assay
index), and you submit each assay's Snakefile. The uniform case here is just the single-assay
degenerate one — same artifacts, no subdirectory. See
[the two artifacts](../concepts/artifacts.md#one-project-one-or-more-datasets) for why a project splits
this way.

---

## What to read next

- [The two artifacts](../concepts/artifacts.md) — why `manifest.yaml` and `processing.yaml` are separate
  files with separate lifetimes.
- [When it refuses](../concepts/refusal.md) — the exit-code contract, and why a refusal is cheap and a
  confidently wrong manifest is not.
- [How a dataset is identified](../concepts/identifying.md) — naming data by its hash, and what moves a hash.
