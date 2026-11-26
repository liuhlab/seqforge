# The study workflow

A **study** is one piece of research: the experiments one paper reports, or one project that has
no paper yet. Public archives do not store studies. GEO (the Gene Expression Omnibus, a public
archive of sequencing data) stores **Series**. One paper's data can span several Series, and a
Series links to papers and to other Series that do not all belong to the same study.

This page gives the chain of steps that takes one study from its database records to two files:

- [`bounds.yaml`](formats/bounds.md) holds the study's **bounds**: its name, the Series that belong to it, its papers,
  and each linked record that was left out, with the reason. A study is whole Series. No step
  splits a Series by sample.
- [`reading.yaml`](formats/reading.md) holds the **study reading**: what was done to each sample, and how the samples
  relate. There is one reading per study.

The chain is built and checked on the **standard set**, a fixed list of 50 small GEO studies. For
those 50, both files sit in the study's folder, `evals/standard-set/<ID>/`. `<ID>` is the Series
the study was picked by, such as `GSE100021`.

The work is planned in stages, and one step is not built yet: stage 6 runs the whole chain once
with no person involved. That step already has its name, its input and its output below.

## The steps in order

| # | Step | Command | Reads | Writes | State |
| --- | --- | --- | --- | --- | --- |
| 1 | Bound the study | `seqforge pass start study-bounds`, then `seqforge study write-bounds` | The records linked to the study, and its papers' abstracts | `bounds.yaml` | Built |
| 2 | Check the bounds | `seqforge study check-bounds <file>` | One `bounds.yaml` | No file. It prints whether the file is accepted | Built |
| 3 | Read the study | `seqforge pass start study-reading`, then `seqforge study write-reading` | The study's `bounds.yaml`, its records, and its papers in full or by their abstracts | `reading.yaml` | Built |
| 4 | Check the reading | `seqforge study check-reading <file>` | One `reading.yaml` | No file. It prints whether the file is accepted | Built |
| 5 | Derive the relations | `seqforge study relations <reading.yaml>` | One `reading.yaml` | No file. It prints the relations | Built |
| 6 | Run the chain unattended | Command not named yet | About 2,000 GEO Series | For each study, a `bounds.yaml` and a `reading.yaml`, or the reason the study stopped | Not built yet. Stage 6 builds it |

Steps 1 to 5 handle one study. Step 6 is the same chain, run over many studies at once.

The 50 studies of the standard set got their own bounds before step 1 was built, from commands
that are now retired. [How the standard set got its bounds](#how-the-standard-set-got-its-bounds)
lists them. The checks run like this, on one study of the standard set:

```bash
seqforge study check-bounds evals/standard-set/<ID>/bounds.yaml
seqforge study check-reading evals/standard-set/<ID>/reading.yaml
seqforge study relations evals/standard-set/<ID>/reading.yaml
```

## Rules that every step follows

**One format at a time.** Every `bounds.yaml` and every `reading.yaml` starts with the line
`format: <number>`. Each check accepts only the current number. It rejects a file that has any
other number, and its message names the number that it expected. A change to a format rewrites
every existing file in the same change, so no file stays behind in an older format.

**A person never blocks a step.** A model writes, a model checks, and a person can read the
results on a view-only page on the side. No step waits for a person's answer. A study that the
chain cannot settle keeps its best answer with its probability, or the chain lists the study with
the reason it stopped. If a person does answer a question while checking, the answer is saved as a
small plain file: the question, the study, the answer, who gave it and when.

**No step reads sequencing data.** Every step works from database records and papers.

## 1. Bound the study

Bounding is drawing a study from its linked records. The bounding job runs a pass of the
`study-bounds` kind, which asks a model the same kind of question once for each study and keeps
each answer in the pass's task folder. [Passes](passes.md) describes passes.
A study is named by the GEO Series it starts from. With no `--keys` file, the pass runs the 50
studies of the standard set.

```bash
seqforge pass start study-bounds --ceiling-usd 0.1
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml
```

**What it reads.** The job reads liulab-data, the lab package that stores the archive records and
the papers, from `LABDATA_STORE` and `LABDATA_PAPERS`. For each study it gathers the **linked
group**: the starting Series, every Series one link away from it, and every paper that those Series
link. Four kinds of link count: a SuperSeries and its sub-Series, a shared BioProject, two
BioProjects under one umbrella BioProject, and a shared paper. A reanalysis link counts for nothing,
and so does a paper that only a BioProject links.

**How it decides.** Code keeps the starting Series, or each sub-Series when the study starts from a
SuperSeries, and it leaves every SuperSeries out. Jev, a model that answers yes/no questions with a
probability, is asked about each other Series and each paper twice: once as written, once reversed.
Jev reads the records and each paper's title, journal, date and abstract, and never a full paper.
An answer is yes when Jev's p(yes) is above 0.5, and no when it is below 0.5. At exactly 0.5 it is
neither. A record is in the study when Jev's answer as written is yes. The two answers agree when
one is yes and the other no. Each claim is `inferred`, and its text gives Jev's p(yes) to each of
the two questions. A kept paper has no probability in the file, so the task keeps it.

**What it writes.** `seqforge study write-bounds --out <folder>` writes each study's answer as
`<folder>/<ID>/bounds.yaml`, or under the name that `--name` gives. It never overwrites a file. For
the standard set, the job's file is `job-bounds.yaml`, beside the set's own `bounds.yaml`. Each
study ends in one of three ways:

| Ending | When | File |
| --- | --- | --- |
| Settled | Jev's two answers agree on every record | Written |
| Unsettled | Jev's two answers do not agree on a record | Written, with each probability |
| Refused | The group holds more than 50 Series, Jev gives no answer about a record, or the bounds fail the format check | None. `write-bounds` prints the reason, such as `group too large` |

### How the standard set got its bounds

This is history: these commands are retired. A small model drafted each of the 50 studies' bounds
in a pass of the `standard-set-bounds` kind. The `standard-set draft` command wrote each answer as
`<ID>/draft.yaml`. The `standard-set judge` command asked a large model about one draft and wrote
`<ID>/verdict.yaml`, `<ID>/bounds.yaml` and `<ID>/study.yaml`. `seqforge standard-set list` still
prints one JSON object per study. The
[standard set's own page](https://github.com/liuhlab/seqforge/tree/main/evals/standard-set) says
what each file holds.

## 2. Check the bounds

`seqforge study check-bounds <file>` reads one `bounds.yaml` and nothing else. It opens no archive
and calls no model. It prints one JSON object that says whether the file is accepted, and for each
problem it names the field and the reason. It exits 0 for an accepted file, 3 for a rejected file
and 2 for a file that it cannot read.

The check asks only whether the file is in the current format. It does not judge whether the
bounds are right. The bounding job never writes a file that the check rejects: it ends the study
refused, and `write-bounds` checks the exact text it writes, with each model named, before the file
appears. The reader reads each study's bounds file as it is.

## 3. Read the study

The reader writes one study's **study reading**: what each sequenced sample is, and which labels
the study varied. It runs a pass of the `study-reading` kind, one study per unit, and keeps each
answer in the pass's task folder. With no `--keys` file, the pass runs the 50 studies of the
standard set.

```bash
seqforge pass start study-reading --ceiling-usd 2 --units-at-once 2
seqforge study write-reading --out evals/standard-set --name job-reading.yaml
```

With `--units-at-once 2`, at most eight model requests are in flight at once.

**What it reads.** The reader reads the study's own `bounds.yaml`, and liulab-data from
`LABDATA_STORE` and `LABDATA_PAPERS`: the records of the Series that the bounds keep, the sequenced
samples of those Series, and the bounds' papers. It reads a paper's full text where the paper store
holds one, and the abstract where it does not. Only a sequenced sample gets a row, so a microarray
sample is left out. The reader never reads a supplementary file of GEO or of a paper. A label that
only such a file gives is `unknown`.

**How it decides.** Two models and code share the work:

1. A writing model, DeepSeek Flash with thinking on, reads the records and the papers. It lists the
   **factors**, which are the labels that the study varied on purpose, with their levels. It also
   lists every value that each label takes, each with a quote. It does not say which sample has
   which value. An answer that cannot be used is asked for once more. The request sends no JSON
   schema, because the gateway refuses a DeepSeek schema with thinking on. Code takes the JSON
   object out of the reply's text instead.
2. Jev, a model that answers closed questions with a probability, answers every closed question. It
   says whether each factor was varied on purpose, which level is the baseline, and whether the
   levels are in order. Then, for each sample, it picks the sample's level on each factor and its
   value on each label from the lists. Some questions are asked twice: plain and negated, or with
   the options in order and reversed.
3. Code looks for every quote in its record, character for character, or in its paper once each
   run of white space is one space. Jev judges whether each found passage backs its claim, or says
   otherwise. A claim is `quoted` only from a found passage that backs it. Otherwise it is
   `inferred`.
4. Code writes the reading and runs the format check. It drops a level of a factor that no sample
   is at.

**Assay guides.** An **assay guide** is a short text that the reader adds to the writer's
instructions and to Jev's questions for a study whose records name its assay. Only pull-down
assays have one: ChIP-seq, CUT&RUN and CUT&Tag. It says that the target is a label and never a
factor. It also asks the writer to tie each control library to its pull-down libraries by the words
that tie them, such as a replicate number in the sample titles. Jev checks each tie. Libraries that
are tied share one `biosample` id.

**Ontology ids.** The ontologies are liulab-data's **ontology store**, found through
`LABDATA_ONTOLOGIES` or the per-user pointer that `labdata ontology init` writes. The pass opens
the store once when it starts, and the store keeps each vocabulary's snapshot fixed until the pass
ends. If the store cannot be opened, `pass start` exits 2 and names `LABDATA_ONTOLOGIES`; it never
reads with no search.

The species id is the record's own taxonomy id, and `healthy` is `PATO:0000461`. The store names
both, and an id it does not hold is not written. A `development_stage` of `adult` is the species'
adult stage in its stage list, found by its whole name. For the mouse, that is MmusDv's
`mature stage`. Every other id comes from a search of the store:
up to 10 candidates, whole-text matches first, and Jev picks one or none. A term is written only
when Jev is sure of its pick. A vocabulary that the store does not hold finds nothing, so its
labels get no id. The reading's `ontology_snapshot` names each snapshot that its ids came from,
such as `liulab-data ontology store: efo-3.96.0, ncbitaxon-2026-11-25, uberon-2026-11-05`. Its date
is the UTC day the pass opened the store, because the store names its snapshots by UTC day.

**What it writes.** `seqforge study write-reading --out <folder>` writes each study's answer as
`<folder>/<ID>/reading.yaml`, or under the name that `--name` gives. It never overwrites a file,
and it writes no file whose exact text the format check rejects. For the standard set, the job's file is `job-reading.yaml`, beside the five worked examples. Each
study ends in one of three ways:

| Ending | When | File |
| --- | --- | --- |
| Settled | Jev's two answers agree on every question asked twice | Written |
| Unsettled | Jev's two answers do not agree on whether a factor was varied on purpose, on its baseline, or on any sample's level. A missing answer, or a probability of exactly 0.5, never agrees | Written, with each probability |
| Refused | The writer's answer cannot be used after one retry, or the reading fails the format check | None. `write-reading` prints the reason |

## 4. Check the reading

`seqforge study check-reading <file>` reads one `reading.yaml` and nothing else, and it accepts or
rejects the file. For each problem it names the field and the reason.

The check looks at the structure of the file. Every sample must have every label, every claim must
say how it is backed in exactly one way, and every passage that a claim cites, for it or against
it, must be in the file. A file that writes an ontology id must name where its ids came from.
The check does not compare a quote with the record that the quote came from. That comparison needs
the records, so it belongs to step 3.

## 5. Derive the relations

`seqforge study relations <reading.yaml>` reads one reading and prints how its samples relate. Code
works the relations out from the labels in the reading each time the command runs. No file stores
them, so a relation is never out of date with its reading.

The command prints five kinds of relation:

| Relation | Meaning |
| --- | --- |
| Contrasts | Two groups of samples that differ in one factor, with the other factors equal. One group is at the baseline, such as untreated |
| Replicates | Samples that have the same assay, the same target, the same control type and the same level of every factor |
| Series | Samples in order along a factor whose levels have an order, such as time points or doses. This kind of series is not a GEO Series |
| Shared biosamples | Samples that were made from the same biosample, such as two assays on one piece of tissue |
| Signal-to-control pairs | A library that has a target, paired with its background control library, such as an input library, made from the same material |

## 6. Run the chain unattended

**Not built yet.** Stage 6 builds this step, and its command is not named yet.

This step runs the chain once over about 2,000 GEO Series, which is about 1% of the sequencing
Series in GEO. The Series are drawn from liulab-data's store, and the 50 studies of the standard
set are kept out. The chain bounds and reads every study with no person involved.

For each study the run writes a `bounds.yaml` and a `reading.yaml`, or it lists the study with the
reason it stopped. Every study ends as settled, unsettled or refused, as
[Passes](passes.md#start-a-pass) defines those endings. No study is sent
to a person.

## Files that exist today

[Files from the hand work](hand-work-files.md) lists every bounds file, reading and verdict made
so far, with where each is, its format and who made it.
