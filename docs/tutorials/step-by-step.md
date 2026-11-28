# Compile a dataset step by step

This tutorial compiles a real dataset one command at a time, so you can open what each step wrote.
You start with FASTQ files and end with a Snakefile, the pipeline you run to get one count matrix
per sample. seqforge does not run that pipeline for you. It writes files, and the last one is the
pipeline.

If you only want the result, `seqforge run` does every step in one command. It comes first below.
[Compile with Claude](with-claude.md) lets an assistant run the commands for you.

## The dataset

The example is the dataset seqforge was built against:
[PRJNA1027859](https://www.ncbi.nlm.nih.gov/bioproject/PRJNA1027859). It holds six single-nucleus
RNA-seq runs of adult *C. elegans* neurons, wild type against *daf-2* mutants, from
[Cell Genomics 2024](https://doi.org/10.1016/j.xgen.2024.100720).

```text
data/
  SRX24283130/SRR28716558_1.fastq.gz    12 files, one folder per experiment,
  SRX24283130/SRR28716558_2.fastq.gz    as fasterq-dump wrote them
  ...
info/
  paper.pdf                             optional
```

The accession, `PRJNA1027859`, is optional too. Much sequencing data never had one. Each input
makes the answer richer, and seqforge checks every one of them against the others.

## What each step needs

Most steps run on your machine with nothing else. Three need something outside it:

| Step | Command | Needs |
| --- | --- | --- |
| All steps in one | `seqforge run` | The network for an accession; the model gateway for a document |
| Fetch the archive records | `seqforge io records` | The network |
| Ask the bytes | `seqforge resolve score` | Nothing |
| Read the prose | `seqforge harvest extract` | The model gateway |
| Write the manifest | `seqforge manifest fill` | The network for `--accession`, unless the records are already fetched |
| Write the recipe | `seqforge processing new` | Nothing |
| Write the Snakefile | `seqforge compose` | Nothing (it can fetch a barcode list it does not ship) |
| Join a study reading | `seqforge project metadata --study` | The lab's data package, to check the study folder |
| Run the Snakefile | `snakemake` | The genome files and the aligner image; your own machine |

The model gateway is the lab's server for language-model calls. To use it, open its tunnel and set
`$LABLLM_GATEWAY` to the local address, as
[Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md) describes. You
need no model provider key.

## All steps in one command

```bash
pixi run -- seqforge run data/*/*.fastq.gz \
    --accession PRJNA1027859 --doc info/paper.pdf \
    --assembly ce11 --annotation WS298 --fastq-dir data
```

`seqforge run` runs the steps below in order: records, harvest, manifest, recipe, Snakefile. It
stops at the first refusal and prints one JSON summary. Its `snakefile` field is the path of the
pipeline. It writes everything under `seqforge/` in the current folder; `-C <folder>` picks another
workspace.

- The genome is the one option with no default. `--assembly` is a UCSC genome id (the assembly's name in the UCSC Genome Browser). `--annotation`
  is the name of a gene annotation registered with the lab's genome package. A wrong genome still
  aligns and exits with no error, so seqforge does not guess.
- `--doc` names a paper or a sample sheet to read. Give it once per document. Reading uses the
  model gateway and spends at most $1.00 unless `--ceiling-usd` says otherwise.
- `--no-llm` skips reading entirely. The manifest is then quieter, and still true.
- Run it again to resume. Each step keeps its own cache, so there is no `--resume` option.

The rest of this page runs the same steps one at a time.

## Fetch the archive records

Needs the network.

```bash
pixi run -- seqforge io records PRJNA1027859
```

```json
{
  "records": "seqforge/records/PRJNA1027859.json",
  "query": "PRJNA1027859",
  "source": "ncbi-sra",
  "n": { "project": 1, "sample": 6, "experiment": 6, "run": 6 },
  "submitted_files": ["..."]
}
```

The archive has four levels, each linked to the one above by an accession:

```text
BioProject PRJNA1027859        title, abstract, submitting centre, data type
  BioSample SAMN40935621         the sample as submitted: strain, tissue, sex, stage
    Experiment SRX24283135         instrument, library strategy, the protocol paragraph
      Run SRR28716553                the run alias, and your files
```

This command copies what the submitter typed and decides nothing. A later step uses the run and
sample records to match each of your files to its sample.

### If your data has no accession

Draft a record set from the file names instead. This runs on your machine.

```bash
pixi run -- seqforge records new reads/ -o records.yaml
pixi run -- seqforge records validate records.yaml
```

The draft declares one sample per run, the grouping seqforge already takes from the file names, so
using it unedited changes nothing. Where two runs differ only in a sample-sheet token or a flowcell
id, it adds a comment: the file names cannot say whether those runs are one sample. Edit the file to
say so, then check it again with `records validate`. Pass it to the manifest step as `--records`.

A record set holds structure only: which files are which run, and which runs are which sample. It
holds no strain, tissue or genotype, because a fact typed there has no document behind it to check.

## Ask the bytes what the library is

Needs nothing.

```bash
pixi run -- seqforge resolve score data/SRX24283130/*.fastq.gz
```

This reads the first 2,000 reads of each file and never more. Two limits bound every read of a
FASTQ file: `--max-reads` and a 256 MB cap on decompressed bytes (`--max-bytes`). A 40 GB file
costs the same as a 40 MB one. Raise `--max-reads` to look at more of a file.

It scores each technology in the [knowledge base](../kb/index.md) against what it saw. It prints the
ranked candidates, the role it gave each file (barcode read, cDNA read), and the step of checks that
settled it. Add `--explain` to see the evidence behind the ranking.

It does not read meaning into file names. `_1` and `_2` are the order `fasterq-dump` wrote the
files in, and say nothing about which read holds the barcode. The bytes decide the roles.

This step is optional: the manifest step runs the same scoring. It exits 3 when the data blocks a
decision, and 4 when the bytes cannot settle a question and a person must.

## Read the prose

Needs the model gateway. Optional.

```bash
pixi run -- seqforge harvest extract info/paper.pdf --records seqforge/records/PRJNA1027859.json
```

This is the only compile step that uses a language model. The model proposes claims, and code
decides which ones stand. Each claim carries a quote from the document. Code finds that quote again
in the document's text and checks that it supports the value. A claim that fails either check is
dropped.

- From a paper, the model is asked for the chemistry, whether the input was cells or nuclei, and
  the organism.
- `--records` adds each experiment's protocol paragraph as its own document, and asks it for the
  chemistry only. Sample, run and project records are asked nothing.
- A chemistry claim is a hint, never evidence. It can break a tie the bytes cannot. If the paper
  and the bytes disagree, seqforge asks you rather than picking.
- `--dry-run` prints what would be asked, of which document, and an estimate of the cost. It
  reaches no gateway.
- `--ceiling-usd` caps the spend, $1.00 by default. Reaching it stops the step with exit 3.

The verified claims go to `seqforge/logs/assertions.json`. The text of each document goes to
`seqforge/records/documents/`, so you can find every quote yourself.

What each sample was (its strain, tissue or treatment) is not a compile question. A study reading
answers it; see [Join a study reading](#join-a-study-reading) below.

## Write the manifest: what the data is

Needs the network for `--accession`, unless the records are already in `seqforge/records/`.

```bash
pixi run -- seqforge manifest fill data/*/*.fastq.gz --accession PRJNA1027859 \
    --assertions seqforge/logs/assertions.json
```

Two parts of seqforge answer two questions here, and neither sees the other's input. The scoring
reads the bytes and decides the library: the chemistry, and which file is which read. The metadata
step reads the archive records and decides which sample each file is. Either can refuse.

You get `seqforge/manifest.yaml`. An excerpt:

```yaml
experiment:
  organism:
    value: 6239
    basis: asserted
    evidence: [SAMN40935616, SAMN40935617, SAMN40935618,
               SAMN40935619, SAMN40935620, SAMN40935621]
    confidence: null
    rung: 0
  samples:
    - sample_id: SAMN40935616
      accession: SAMN40935616
      file_uris: [SRX24283130/SRR28716558_1.fastq.gz, SRX24283130/SRR28716558_2.fastq.gz]
    # ... five more
```

Two things to notice.

- **You did not pass `--organism`.** The sample records declare it, and the manifest cites the six
  BioSamples it came from. A flag still wins if you pass one. There is no default organism, because
  a wrong one aligns against the wrong genome and exits with no error.
- **`confidence: null` means nothing was judged.** seqforge copied the taxonomy id from the records.
  `basis: asserted` and the accessions in `evidence` say how it knows. A number would claim a
  judgement that nobody made.

The manifest takes no genome. The genome is a choice about what to do with the data, so it goes in
the recipe. seqforge writes `manifest.yaml` only after the manifest passes its own checks;
`seqforge manifest validate seqforge/manifest.yaml` runs those checks again.

### With no accession

```bash
pixi run -- seqforge manifest fill reads/*.fastq.gz --organism 6239
```

Files are grouped into runs by name, and each run becomes a sample. Add `--records records.yaml` if
you drafted a record set.

### When the accession does not match your files

seqforge refuses:

```text
RECORD_JOIN_INCOMPLETE: PRJNA1027859 declares 6 run(s) (SRR28716553, ...), and 1 file(s) on disk
match none of them by run accession or by the original filenames the record declares: mystery_1.fastq.gz
```

Joining only some files would leave one file with no sample, while the manifest still read as if it
described everything.

## Write the recipe: what to do with the data

Needs nothing.

```bash
pixi run -- seqforge processing new seqforge/manifest.yaml \
    --assembly ce11 --annotation WS298 --out seqforge/processing.yaml
```

A dataset has one manifest. It can have many recipes, one for each way you process it. To align the
same data to another genome, write another recipe. Never edit `manifest.yaml` to change how data is
processed.

With no other options, the recipe counts all five STARsolo features in one pass. STARsolo is the
aligner's single-cell mode. Single-nucleus data is full of unspliced pre-mRNA and needs the
`GeneFull` feature, which the default already counts, so this costs you no decision. `--quantify`
replaces the list, and narrowing it warns you.

## Write the Snakefile

Needs nothing, unless a barcode list is missing from the package and must be fetched.

```bash
pixi run -- seqforge compose seqforge/manifest.yaml \
    --processing seqforge/processing.yaml --fastq-dir data
```

```json
{
  "snakefile_path": "seqforge/pipeline/default-<run-id>/Snakefile",
  "config_path": "seqforge/pipeline/default-<run-id>/config.yaml",
  "units_path": "seqforge/pipeline/default-<run-id>/units.tsv",
  "gate": {
    "params": { "status": "pass", "reason": [] },
    "wiring": { "status": "pass", "reason": [] },
    "e2e": { "status": "skip", "reason": ["the end-to-end toolchain is incomplete here: ['STAR'] absent"] }
  }
}
```

The folder is named for the recipe (`default`) and the first 12 characters of the run id. The run id
is a hash of the dataset, the recipe, the knowledge base and the workflow. Compiling the same data a
second way gives a second folder, and the dataset's own hash does not move. A new seqforge release
also changes the run id, so take the path from your own output, not from this page.

`--fastq-dir` tells the pipeline where this machine keeps the FASTQ files. Without it, the pipeline
cannot find its input. `--processing` is optional: without it, compose uses the default recipe and
takes `--assembly` and `--annotation` directly.

Compose runs three checks, called gates, and exits 3 if one fails:

- **params** checks the aligner settings against the reads. Is the UMI length 12 when the reads
  carry a 12 bp UMI? Does the cDNA read sit where the aligner expects it? A wrong setting here does
  not crash. STARsolo exits 0 and writes a matrix that looks like a thin dataset.
- **wiring** dry-runs the Snakefile with `snakemake -n` and checks that it plans jobs. A dry run
  that plans nothing also exits 0, so the gate reads the plan.
- **e2e** is the real count-matrix run. It reports `skip` with its reason when the aligner or the
  genome is missing, never a `pass` it did not earn.

## Join a study reading

Needs the lab's data package installed, to check the study folder. Optional.

A compile records which sample each file is, not what each sample was. A study reading records what
each sample was: its tissue, genotype or treatment, each claim with its source. The separate study
chain writes the reading into a study folder; [Read your own studies](../guides/read-studies.md)
shows how.

```bash
pixi run -- seqforge project metadata --study <study-folder>
```

This rebuilds `seqforge/sample_metadata.tsv` with the reading joined in. The join matches each
manifest sample's BioSample accession to the BioSample accession on each reading row.

- Each factor, and each label that differs between samples, gets its own column. A last column,
  `reading_hash`, names the reading the labels came from.
- A mismatch only warns: a sample with no reading row, a reading row with no compiled sample, or a
  species or assay that differs from the manifest's. The command still exits 0.
- The manifest and its hash do not change.
- A study folder that fails its format checks exits 2 before anything is written.

`seqforge run --study <study-folder>` does the same join as part of a compile. Neither command starts
the study chain. After a newer reading, run `project metadata --study` again and then run the same
Snakefile. Only the step that labels each matrix runs again.

## Run the Snakefile

The folder to run in is the one compose printed: its `snakefile_path` without the `Snakefile` at the
end.

```bash
cd seqforge/pipeline/<the-folder-compose-named>
snakemake --software-deployment-method apptainer
```

This part is yours. seqforge does not submit jobs and has no opinion about your scheduler.

- `--software-deployment-method apptainer` runs the aligner inside the pinned lab software image.
  Without it, snakemake ignores the image and takes STAR from your `PATH`. The run still works, but
  the software is no longer pinned.
- **Run it on one machine.** The pipeline loads the genome index into shared memory once, and every
  mapping job on that machine uses the same copy. On a human genome that copy is about 31 GB. Spread
  across machines, each job loads its own. To go faster, run several pipelines at once rather than
  spreading one out, and let `--cores` set how many samples map at a time.
- The default target asks for each sample's finished outputs: one `<sample>.h5ad` count matrix and a
  `<sample>.velocyto.h5ad` of spliced and unspliced counts, together with a QC summary. If you
  joined a reading, each `.h5ad` carries the sample's labels as columns.
- The barcode list is not stored in the pipeline folder. A step writes it, STAR reads it, and
  snakemake deletes it.

## Write a report

```bash
pixi run -- seqforge report
```

This writes `seqforge/report.html`, one page that opens with no network, and `seqforge/report.json`,
the data the page is built from. Once the pipeline has run, the report adds each sample's results.
`--study <study-folder>` adds the study's own tab.

## When the data mixes technologies

Every run of this dataset is the same 10x chemistry, so it compiled to one manifest and one
Snakefile. A series that mixes technologies, for example 10x v2 and v3 libraries, compiles to one
manifest and one Snakefile per technology, each in `seqforge/<assay>/`. The bytes decide the split.
`sample_metadata.tsv` and `project.yaml` stay at the top of `seqforge/` and cover every sample. You
run each assay's Snakefile on its own.

## Read next

- [From FASTQ files to a pipeline](../how-it-works/compiler.md): why the manifest and the recipe are
  separate files.
- [Why you can trust the output](../how-it-works/trust.md): what each refusal means.
- [Commands](../reference/commands.md): every command and its exit codes.
