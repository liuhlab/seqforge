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

The work is planned in stages, and three steps are not built yet. Stage 4 builds the bounding job,
stage 5 builds the reader, and stage 6 runs the whole chain once with no person involved. Each of
those steps already has its name, its input and its output below.

## The steps in order

| # | Step | Command | Reads | Writes | State |
| --- | --- | --- | --- | --- | --- |
| 1 | Bound the study | Command not named yet | The records linked to the study | `bounds.yaml` | Not built yet. Stage 4 builds it |
| 2 | Check the bounds | `seqforge study check-bounds <file>` | One `bounds.yaml` | No file. It prints whether the file is accepted | Built |
| 3 | Read the study | Command not named yet | The study's `bounds.yaml`, its records and its papers | `reading.yaml` | Not built yet. Stage 5 builds it |
| 4 | Check the reading | `seqforge study check-reading <file>` | One `reading.yaml` | No file. It prints whether the file is accepted | Built |
| 5 | Derive the relations | `seqforge study relations <reading.yaml>` | One `reading.yaml` | No file. It prints the relations | Built |
| 6 | Run the chain unattended | Command not named yet | About 2,000 GEO Series | For each study, a `bounds.yaml` and a `reading.yaml`, or the reason the study stopped | Not built yet. Stage 6 builds it |

Steps 1 to 5 handle one study. Step 6 is the same chain, run over many studies at once.

The 50 studies of the standard set have bounds although step 1 is not built. Three older commands
drew them, and [How the standard set got its bounds](#how-the-standard-set-got-its-bounds) lists
those commands. The steps that are built today run like this, on one study of the standard set:

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

**Not built yet.** Stage 4 builds this step, and its command is not named yet.

Bounding is drawing a study from its linked records. The bounding job gathers the records linked to
one study: its GEO Series, and the Series and the papers that those link to. Models then decide
which of them belong to the study and which are left out, and they name the study. The job writes
the result as one `bounds.yaml`.

### How the standard set got its bounds

The 50 studies were bounded before the bounding job existed. A small model drafted each study's
bounds, and a large model judged each draft once. The first three commands below did that work, in
this order. The fourth command shows the result.

| Step | Command | Reads | Writes |
| --- | --- | --- | --- |
| Drafting pass | `seqforge pass start standard-set-bounds` | The records linked to each study and the abstract of each paper, from liulab-data, the lab package that stores the archive records and the papers | Each study's answer, in the pass's task folder |
| Draft | `seqforge standard-set draft` | The answers of the drafting pass | `<ID>/draft.yaml`, for each study that has none |
| Judge | `seqforge standard-set judge <ID>` | One study's draft and what the drafting model saw. The judge may also open the full records and the full paper texts | `<ID>/verdict.yaml`, `<ID>/bounds.yaml` and `<ID>/study.yaml` |
| List | `seqforge standard-set list` | Each study's `study.yaml` and `bounds.yaml` | No file. It prints one JSON object per study |

A pass asks a model the same kind of question once for each study on a list.
[Passes and reviews](passes-and-reviews.md) describes passes and the task folder. The
[standard set's own page](https://github.com/liuhlab/seqforge/tree/main/evals/standard-set) says
how to run these commands and what each file holds.

## 2. Check the bounds

`seqforge study check-bounds <file>` reads one `bounds.yaml` and nothing else. It opens no archive
and calls no model. It prints one JSON object that says whether the file is accepted, and for each
problem it names the field and the reason. It exits 0 for an accepted file, 3 for a rejected file
and 2 for a file that it cannot read.

The check asks only whether the file is in the current format. It does not judge whether the
bounds are right. What the chain does with a rejected file is not decided yet. Stages 4 and 5
decide it.

## 3. Read the study

**Not built yet.** Stage 5 builds this step, and its command is not named yet.

The reader writes one study's reading from three inputs: the study's `bounds.yaml`, the records of
the Series in those bounds, and the study's papers. It writes one `reading.yaml`.

A reading has one row per sample. The row says which biosample and which donor the sample came
from, which assay was run, and what the library targets or which kind of control library it is. It
also holds general attributes such as species and tissue. The reading names the study's
**factors**, the labels that the study varied on purpose, such as a treatment. Each statement in
the file is a claim. A claim says how it is backed, for example by a passage quoted from a record
or a paper, and it carries a probability.

Until the reader exists, the only readings are worked examples for a few studies of the standard
set. A model wrote each of them.

## 4. Check the reading

`seqforge study check-reading <file>` reads one `reading.yaml` and nothing else, and it accepts or
rejects the file. For each problem it names the field and the reason.

The check looks at the structure of the file. Every sample must have every label, every claim must
say how it is backed in exactly one way, and every passage that a claim cites must be in the file.
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
[Passes and reviews](passes-and-reviews.md#start-a-pass) defines those endings. No study is sent
to a person.

## Files that exist today

[Files from the hand work](hand-work-files.md) lists every bounds file, worked reading and
verdict made so far, with where each is, its format and who made it.
