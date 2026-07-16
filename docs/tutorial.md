# Tutorial: compiling a real dataset

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

```
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
language model, which needs its own `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY` in the environment; add
`--no-llm` to skip prose and stay fully deterministic.

The rest of this page walks the same pipeline one verb at a time — which is what you want when a step
needs inspecting, and what `run` is doing for you when it does not.

## 1. Ask the bytes what this is

```bash
pixi run -- seqforge resolve score data/SRX24283130/*.fastq.gz
```

This reads the **first 200 000 reads** of each file and never more (it is bounded by
`--max-reads` *and* by a 256 MB decompressed cap; a code path that could stream a whole multi-GB
FASTQ is a bug here, not a slow path). It scores every technology in the knowledge base against what
it saw and prints the ranked candidates, the role assigned to each file, and the rung that settled
it.

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
  "source": "ncbi-sra+biosample",
  "n": { "project": 1, "sample": 6, "experiment": 6, "run": 6 }
}
```

Four levels, joined by the archive's own accessions:

```
BioProject PRJNA1027859      title, abstract, centre, data type
  └─ BioSample SAMN40935621     strain=CQ758, tissue=Neurons, sex=hermaphrodite, dev_stage=Adult Day 1
       └─ Experiment SRX24283135   instrument, library strategy, and the protocol paragraph
            └─ Run SRR28716553        the alias "Rep3 daf2 reads", and your files
```

This verb is a **transcriber**. It reports what the submitter typed and stops; it decides nothing.

**This is where per-sample metadata comes from, and it is the part seqforge used to throw away.**
`strain`, `tissue`, `sex` and `dev_stage` live on the BioSample record. The fields you get from
`io resolve` are byte-identical across all six runs of this study — every one of them says "Model
organism or animal sample from Caenorhabditis elegans" — so they can tell you nothing about any
individual sample.

## 3. Write down what the data IS

```bash
pixi run -- seqforge manifest fill data/*/*.fastq.gz --accession PRJNA1027859
```

Two resolvers run here, and they answer different questions.

`resolve score` reads the **bytes** and decides the library: which chemistry, which file is which
read. The metadata resolver reads the **records** and decides which sample each file is and what that
sample was. Neither is shown the other's input — the metadata resolver is handed file names and
hashes, not the probe's output, because there is no byte in a FASTQ that bears on `tissue`.

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
  study:
    accession: PRJNA1027859
    title: A Single-Nucleus Atlas of Adult C. elegans Neurons Reveals GPCR and Insulin-signaling Profiles
    center: Princeton University
    data_type: raw sequence reads
  samples:
    - sample_id: SAMN40935616
      accession: SAMN40935616
      attributes:
        strain:    {value: CQ757,         basis: asserted, evidence: [SAMN40935616], confidence: null, rung: 0}
        tissue:    {value: Neurons,       basis: asserted, evidence: [SAMN40935616], confidence: null, rung: 0}
        sex:       {value: hermaphrodite, basis: asserted, evidence: [SAMN40935616], confidence: null, rung: 0}
        dev_stage: {value: Adult Day 1,   basis: asserted, evidence: [SAMN40935616], confidence: null, rung: 0}
      file_uris: [SRX24283130/SRR28716558_1.fastq.gz, SRX24283130/SRR28716558_2.fastq.gz]
    # ... five more
```

Three things worth reading closely.

**You did not pass `--organism`.** The record declares it, and the manifest cites the six BioSamples
it came from. A flag still wins if you pass one — a human typing a taxid is asserting it now, having
looked — but nothing is defaulted. There is no default taxid and there must not be: a wrong one
aligns cleanly against the wrong genome, exits 0, and nothing downstream ever asks again.

**`confidence: null` is the informative value.** It means nothing was judged. Copying `strain: CQ758`
out of a BioSample record is a transcription — `basis: asserted` plus the record accession in
`evidence` already says everything true about how we know it. A `1.0` there would invite the question
it cannot answer ("you are certain the strain is CQ758?"). We are certain the *record declares* it,
which is a different claim and the one we make.

**The attribute keys are NCBI's, not ours.** `strain`, `tissue`, `dev_stage` are three of NCBI's 960
harmonized BioSample attribute names, each with a definition somebody else maintains
(`seqforge io attributes tissue` will read it to you). There is no `condition`: that was ours, no
archive defines it, and a field named "condition" accepts anything you can call a condition.

### If you have no accession

```bash
pixi run -- seqforge manifest fill reads/*.fastq.gz --organism 6239
```

That works, and it is the common case. Files are grouped into runs by name, each run becomes a
sample, and the samples carry no facts. Exit 0. A quieter manifest, and just as true.

### If the accession does not match your files

That is a refusal, not a shrug:

```
RECORD_JOIN_INCOMPLETE: PRJNA1027859 declares 6 run(s) (SRR28716553, ...), and 1 file(s) on disk
match none of them by run accession or by the original filenames the record declares: mystery_1.fastq.gz
```

Half-joining would leave that file with no sample facts while the manifest still read as though it
described everything.

## 4. Let the model read the prose (optional)

```bash
pixi run -- seqforge harvest extract info/paper.pdf --records seqforge/records/PRJNA1027859.json
pixi run -- seqforge manifest fill data/*/*.fastq.gz --accession PRJNA1027859 \
    --assertions seqforge/assertions.json
```

This is the **only** stage that touches a language model. It has exactly two jobs: turn prose into
claims that carry a quote, and arbitrate an ambiguity code has already flagged. Everything else in
seqforge is a verifier.

Every claim it returns carries a verbatim quote. Code — not the model — greps that quote back into
the normalized document, computes the offsets, and checks that the quote actually supports the value.
A claim that fails either check is discarded.

**How a claim names a sample.** It doesn't. `--records` renders each archive record as its own
document, so the document for SAMN40935621 contains that sample's prose and nothing else — which
means "which sample" is answered by *which file we handed the model*. Code knows it because code
chose it. The model is never asked to name a sample and could not.

The ask is scoped the same way. A BioSample's document is asked for sample attributes and never for a
chemistry; an experiment's protocol paragraph is asked for the chemistry and nothing else; the study
abstract is read by nobody, because "wild-type and daf-2 mutants" is true of the study and false of
every individual sample in it.

**Two sources disagreeing is decided by precedence, then noted — not voted, and not a refusal.** If
the paper says "we dissected neurons and body wall muscle" and the model comes back with
`tissue=muscle`, that quote is real and it does entail the value — the span check passes, and it is
*right to* pass. What catches it is the record saying `Neurons`: the record is a declaration about
that sample (`asserted`), the paper's reading is our inference (`inferred`), so **the record's value
stands** and the paper's is surfaced as a non-blocking **warning**. The manifest still compiles — a
single ambiguous sample annotation is no reason to refuse a dataset. And if two *equal* authorities
disagree (two records, say), the field is left **null**: a missing value is not permanent, a wrong one
is. Only a disagreement about what the data *is* — the byte-level chemistry — blocks.

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
  "gate": { "params": "pass", "wiring": "pass", "e2e": "skip" }
}
```

The directory is named for the recipe plus the first 12 characters of the run id. The run id is
`H(dataset ⊕ processing ⊕ kb ⊕ workflow)`, so compiling this dataset a second way gives you a second
directory rather than silently overwriting the first — and the dataset's own hash does not move.

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

```bash
cd seqforge/pipeline/default-d94c737eb677
snakemake --profile <your-cluster-profile> --software-deployment-method apptainer
```

**This part is yours.** seqforge has no opinion about your scheduler, and it will not grow one.

`--software-deployment-method apptainer` is what makes the alignment rule run inside the pinned
`liulab-runtime` image. Without it snakemake ignores the `container:` directive entirely and STAR
comes from your `PATH`: the run still works, and it is no longer pinned.

The default target is `all`, which demands the matrices — one `<sample>.h5ad` per sample, plus a
`<sample>.velocyto.h5ad` — not a folder that might be empty.

The 111 MB barcode whitelist is not in that directory, and should not be. A rule builds it, STAR
reads it once, and snakemake deletes it.

---

## What to read next

- [The two artifacts](concepts/artifacts.md) — why `manifest.yaml` and `processing.yaml` are separate
  files with separate lifetimes.
- [When it refuses](concepts/refusal.md) — the exit-code contract, and why a refusal is cheap and a
  confidently wrong manifest is not.
- [How a dataset is identified](concepts/identifying.md) — content addressing, and what moves a hash.
