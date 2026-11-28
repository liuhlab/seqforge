# The study workflow

This page gives each step of the study chain in order, and the rules each step follows.

**What goes in:** one GEO Series id per study. GEO, the Gene Expression Omnibus, is a public
archive of sequencing data, and a **Series** is one deposit in it, with an id such as `GSE100021`.
The chain reads the Series' archive records and the papers they link. It never reads sequencing
data.

**What comes out:** two files in each study's folder.

- [`bounds.yaml`](formats/bounds.md) holds the study's **bounds**: the Series and the papers that
  belong to it, and each linked record that was left out, with the reason.
- [`reading.yaml`](formats/reading.md) holds the **study reading**: what each sequenced sample
  was, which labels the study varied on purpose, and the quotes that back each statement.

**Why you can check it:** models answer, and code decides whether an answer stands. Code finds
every quote again in its source. Each question that decides an ending is asked twice, in two
forms, and the two answers must agree. Every file passes its format check before it is written.

## Words this page uses

- A **study** is one piece of research: the experiments one paper reports, or one project that has
  no paper yet. A study is whole Series. No step splits a Series by sample.
- A **study folder** holds one study's files. It is named by the Series the study starts from,
  such as `GSE100021`.
- A **folder of study folders** holds many study folders. Its `pick.yaml` lists the studies to
  run. A folder with no `pick.yaml` needs a `--keys` file that names them, one Series per line.
- The **standard set** is a fixed list of 50 small GEO studies in `evals/standard-set/`. The chain
  is built and checked on it, and it is the folder a pass uses when `--studies` names none.
- **Jev** is a model that answers yes-or-no and multiple-choice questions with a probability.
- A **job** is one step that models do for each study: bounding or reading. Each job runs as a
  [pass](passes.md), a run of one kind of work over many studies under a dollar ceiling.

## The steps in order

| # | Step | Command | Needs | Writes |
| --- | --- | --- | --- | --- |
| 1 | Bound the study | `seqforge pass start study-bounds`, then `seqforge study write-bounds` | The gateway, the record store and the paper store | `bounds.yaml` |
| 2 | Check the bounds | `seqforge study check-bounds <file>` | Nothing | No file. It prints whether the file is valid |
| 3 | Read the study | `seqforge pass start study-reading`, then `seqforge study write-reading` | The gateway and the record, paper and ontology stores | `reading.yaml` |
| 4 | Check the reading | `seqforge study check-reading <file>` | liulab-data installed | No file. It prints whether the file is valid |
| 5 | Work out the relations | `seqforge study relations <file>` | liulab-data installed | No file. It prints the relations |
| 6 | Run the chain unattended | `seqforge study run --studies <folder> --ceiling-usd <dollars>` | As steps 1 and 3 | `bounds.yaml` and `reading.yaml` for each study |

The gateway is the lab's model server, reached at `$LABLLM_GATEWAY`. The stores belong to
liulab-data, the lab's data package: the record store (`LABDATA_STORE`), the paper store
(`LABDATA_PAPERS`) and the ontology store (`LABDATA_ONTOLOGIES`). [Passes](passes.md) says how to
set each one.

Steps 1 to 5 handle one study at a time. Step 6 runs steps 1 and 3 over every study of a folder.

## Rules that every step follows

**One format at a time.** Every `bounds.yaml` and every `reading.yaml` starts with the line
`format: <number>`. Each check accepts only the current number, and its message names the number
it expected. A change to a format rewrites every existing file in the same change, so no file
stays in an older format.

**A file is never overwritten.** `write-bounds` and `write-reading` write a study's file only when
none is there. To take a newer answer, delete the old file by hand and write again.

**A file that fails its check is not written.** Each write runs the format check on the exact text
it would write.

**A person never blocks a step.** No step waits for a person's answer. A study the chain cannot
settle keeps its best answer, with each probability, or it is listed with the reason it stopped.
If a person does answer a question while checking, the answer is saved as a small plain file: the
question, the study, the answer, who gave it and when.

**No step reads sequencing data.** Every step works from archive records and papers.

## 1. Bound the study

Bounding draws one study out of its linked records. One GEO Series can link to other Series and to
papers that belong to a different study, such as an older dataset that the study re-uses. The
bounding job decides which linked records belong.

```bash
seqforge pass start study-bounds --ceiling-usd 0.1 --studies <folder>
seqforge study write-bounds --out <folder>
```

The first command needs the gateway, the record store and the paper store. The second reads only
the pass's task. Give both commands the same folder. With no `--keys`, the pass runs the studies
that the folder's `pick.yaml` lists.

### What the bounding job reads

For each study, the job gathers the **linked group**: the starting Series, every Series one link
away from it, and every paper that those Series link. Four kinds of link count:

- a SuperSeries and its sub-Series (a SuperSeries is a Series that groups other Series);
- a shared BioProject, the archive's record of one research project;
- two BioProjects under one umbrella BioProject;
- a shared paper.

A reanalysis link counts for nothing, and so does a paper that only a BioProject links. Jev reads
each Series record with its samples, and each paper's title, journal, date, publication type and
abstract. It never reads a full paper.

### How the bounding job decides

1. Code keeps the starting Series. When the study starts from a SuperSeries, code keeps each of
   its sub-Series instead. Code leaves every SuperSeries out, because a SuperSeries only repeats
   its sub-Series' samples.
2. Jev is asked about each other Series and each paper, twice: once as written, and once
   reversed. For a Series, the question is whether it was deposited for the same research. For a
   paper, the question is whether it is the paper that first reports the data. A preprint and its
   journal version both count.
3. An answer is yes when Jev's probability of yes is above 0.5, and no when it is below 0.5. At
   exactly 0.5 it is neither.
4. A record is in the study when Jev's answer to the question as written is yes. The two answers
   agree when one is yes and the other is no.

Each claim in the file is `inferred`. A claim that code made says so, and has no probability. A
claim that Jev made gives Jev's probability of yes to both questions, and carries `p`. A kept paper
is listed by its id only. Its two answers are kept in the pass's task, because the file has no
place for them.

The job also copies each placed record's title into the file's `titles` map, from the Series
record or the paper. That covers every record kept or left out. A record with no title has no
entry.

### How the bounding job ends a study

`seqforge study write-bounds --out <folder>` writes each study's answer as
`<folder>/<ID>/bounds.yaml`, or under the name that `--name` gives. For the standard set, the
job's file is `job-bounds.yaml`, beside the set's own `bounds.yaml`. While a pass holds the task,
the write refuses and writes nothing: it exits 3 with `stopped: task_in_use` and names the process
that holds the task. Run it again once the pass ends.

| Ending | When | File |
| --- | --- | --- |
| Settled | Jev's two answers agree on every record | Written |
| Unsettled | Jev's two answers do not agree on a record | Written, with each probability |
| Refused | The group holds more than 50 Series, Jev gives no answer about a record, or the bounds fail the format check | None. `write-bounds` prints the reason, such as `group too large: <n> Series` |
| Held | The key is not a GEO Series, the record store does not hold the starting Series, or the study links a paper and no paper store is set | None. `seqforge pass status study-bounds` lists the study under `not_done` |

### How the standard set got its bounds

The 50 studies of the standard set got their own `bounds.yaml` before the bounding job was built,
from commands that are now retired. A small model drafted each study's bounds in a pass of the
`standard-set-bounds` kind. The `standard-set draft` command wrote each answer as
`<ID>/draft.yaml`. The `standard-set judge` command asked a large model about one draft, and wrote
`<ID>/verdict.yaml`, `<ID>/bounds.yaml` and `<ID>/study.yaml`.

`seqforge standard-set list` is the one verb left. It prints one JSON object per study, from its
study file and its bounds file. [Files from the hand work](hand-work-files.md) says what each file
holds.

## 2. Check the bounds

```bash
seqforge study check-bounds <folder>/<ID>/bounds.yaml
```

The check reads one bounds file and nothing else. It opens no store and calls no model. It prints
one JSON object: the file, the format number it accepts, whether the file is valid, and each
problem, naming the field and the reason.

| Exit | When |
| --- | --- |
| 0 | The file is valid. |
| 3 | The file is rejected. |
| 2 | The file cannot be read. |

The check asks only whether the file is in the current format. It does not judge whether the
bounds are right. The reader reads each study's bounds file through the same check. A study whose
file is missing or rejected is held, and its reason names each problem.

## 3. Read the study

The reader writes one study's **study reading**: what each sequenced sample was, and which labels
the study varied. It runs a pass of the `study-reading` kind, one study per unit.

```bash
seqforge pass start study-reading --ceiling-usd 2 --studies <folder>
seqforge study write-reading --out <folder>
```

The first command needs the gateway and the record, paper and ontology stores. It reads each
study's `bounds.yaml` in the folder, so run it after step 1. For the standard set, add
`--name job-reading.yaml` to the write, so the job's file sits beside the set's own five worked
readings.

Each study sends at most 4 requests to Jev at once. So `--units-at-once 2` keeps at most 8 Jev
requests in flight.

### What the reader reads

The reader reads the study's `bounds.yaml`, and from the stores:

- the records of the Series that the bounds keep;
- the sequenced samples of those Series;
- the bounds' papers: the full text where the paper store holds one, and the abstract where it
  does not.

Only a sequenced sample gets a row, so a microarray sample is left out. The reader never reads a
supplementary file of GEO or of a paper. A label that only such a file gives is `unknown`.

### How the reader decides

Two models and code share the work.

1. **A writing model lists the options.** DeepSeek Flash (`deepseek/deepseek-flash`), with
   thinking on, reads the records and the papers. It lists the **factors**, the labels that the
   study varied on purpose, with their levels and baseline. It lists every value that each label
   takes, each with a quote. It does not say which sample has which value. An answer that cannot
   be used is asked for once more.
2. **Jev answers every closed question.** It says whether each factor was varied on purpose,
   which level is the baseline, and whether the levels are in order. Then, for each sample, it
   picks the sample's level on each factor and its value on each label, from the writer's lists.
3. **Some questions are asked twice.** Whether a factor was varied is asked plain and negated.
   Its baseline, and each sample's level, are asked with the options in order and then reversed.
   Each sample's label values are asked once.
4. **Code finds every quote.** A quote from a record must be in its named field, character for
   character. A quote from a paper must be in the paper once each run of spaces and line breaks is
   one space. Jev then judges whether each found passage backs its claim, or speaks against it.
   A claim is `quoted` only from a found passage that backs it. Otherwise it is `inferred`.
5. **Code writes the reading.** It drops a factor level that no sample is at, copies each sample's
   BioSample accession, and runs the format check.

A sample's **BioSample accession** is the `SAMN` id that its GEO record links. Code copies it into
the sample's row as `biosample_accession`, only when the record links exactly one. It is an id,
not a claim. A compile joins a reading into its sample table by this id.

The writer gets no JSON schema, because the gateway cannot send DeepSeek a schema while it thinks.
Code reads the JSON object out of the reply's text instead. The reply must be all JSON, or hold
exactly one fenced block.

### Assay guides

An **assay guide** is a short text that the reader adds to the writer's instructions and to Jev's
questions, for a study whose records name one kind of assay. One guide exists today, for pull-down
assays: ChIP-seq, ChIP-exo, CUT&RUN and CUT&Tag. In these assays an antibody pulls down the
chromatin bound by one protein or histone mark, and a control library measures the background.

The guide says that the target is a label and never a factor. It asks the writer to tie each
control library to the pull-down libraries made from the same material, by the words that tie
them, such as a replicate number in the sample titles. Jev checks each tie. Libraries that are tied
share one `biosample` id. That `biosample` label is an id the reader makes up, such as `B1`. It is
not the BioSample accession.

### Ontology ids

An ontology id names a term in a standard vocabulary, such as `UBERON:0002107`, the liver in
UBERON, a vocabulary of body parts. The ids come from liulab-data's ontology store, found through
`LABDATA_ONTOLOGIES` or the pointer that liulab-data saves. The pass opens the store once when it
starts. The store keeps each vocabulary's snapshot fixed until the pass ends. If the store cannot
be opened, `pass start` exits 2 and says why. It never reads a study with no search.

- The species id is the record's own taxonomy id.
- `healthy` is `PATO:0000461`.
- A `development_stage` of `adult` is the species' adult stage, found by its whole name. For the
  mouse, that is MmusDv's `mature stage`.
- Every other id comes from a search of the store: up to 10 candidates, whole-text matches first.
  Jev picks one or none, and a term is written only when Jev's pick has a probability of 0.8 or
  more.

The store must name an id, or the id is not written. A vocabulary that the store does not hold
finds nothing, so its labels get no id. The reading's `ontology_snapshot` names each snapshot that
its ids came from, such as
`liulab-data ontology store: cl-2026-11-13, efo-3.96.0, ncbitaxon-2026-11-26, pato-2026-10-01`.
Its `date` is the day in UTC (Coordinated Universal Time) when the pass opened the store, because
the store names its snapshots by UTC day.

### How the reader ends a study

`seqforge study write-reading --out <folder>` writes each study's answer as
`<folder>/<ID>/reading.yaml`, or under the name that `--name` gives. It follows the same rules as
`write-bounds`: it never overwrites a file, it writes no file that fails the format check, and it
refuses while a pass holds the task.

| Ending | When | File |
| --- | --- | --- |
| Settled | Jev's two answers agree on every question asked twice, and Jev answered every question asked once | Written |
| Unsettled | Jev's two answers disagree on whether a factor was varied on purpose, on its baseline, or on a sample's level. Or Jev left a question asked once unanswered. A missing answer, or a probability of exactly 0.5, never agrees | Written, with each probability |
| Refused | The writer's answer cannot be used after one retry, the store holds no sequenced sample of the study, the writer lists more than 25 values for one label, or the reading fails the format check | None. `write-reading` prints the reason |
| Held | The key is not a GEO Series, the study's bounds file is missing or fails its check, the record store does not hold a kept Series, or the study has papers and no paper store is set | None. `seqforge pass status study-reading` lists the study under `not_done` |

A question asked once is one of these: a sample's value on a label, a label with no listed value,
where the libraries' material came from, whether a factor's levels are in order, a passage check,
a tie, or a term pick. When Jev leaves one unanswered, its claim is `unknown` with no probability.

## 4. Check the reading

```bash
seqforge study check-reading <folder>/<ID>/reading.yaml
```

The check reads one reading file and accepts or rejects it. It prints the same JSON object as
`check-bounds`, with the same exit codes. It also exits 2, with no verdict, when liulab-data is not
installed, because liulab-data's vocabularies judge each ontology id.

The check looks at the structure of the file:

- every sample has every label;
- every claim says how it is backed, in exactly one way;
- every passage that a claim cites, for it or against it, is in the file;
- a file that writes an ontology id names where its ids came from, and each id is from its label's
  vocabulary.

The check does not look for a quote in the record that the quote names. That needs the records,
so it belongs to step 3.

## 5. Work out the relations

```bash
seqforge study relations <folder>/<ID>/reading.yaml
```

The command checks the file as `check-reading` does, and rejects it the same way. For a valid file,
its JSON also holds `relations`. Code works the relations out from the reading's labels each time
the command runs. No file stores them, so a relation is never out of date with its reading.

| Relation | Key | Meaning |
| --- | --- | --- |
| Replicates | `replicates` | Samples with the same assay, the same target, the same control type and the same level of every factor. |
| Contrasts | `contrasts` | Two groups of samples that differ in one factor, with the other factors equal. One group is at the baseline, such as untreated. |
| Series | `series` | Samples in order along a factor whose levels have an order, such as time points or doses. This kind of series is not a GEO Series. |
| Shared biosamples | `shared_biosample` | Samples made from the same material, such as two assays on one piece of tissue. |
| Signal-to-control pairs | `signal_to_control` | A library that has a target, paired with its background control library made from the same material, such as an input library. |

## 6. Run the chain unattended

```bash
seqforge study run --studies <folder> --ceiling-usd <dollars>
```

This command needs the gateway and all three stores. It runs four commands in order over the
studies of one folder:

1. `pass start study-bounds`;
2. `study write-bounds`, which writes each study's `bounds.yaml`;
3. `pass start study-reading`, which reads the bounds just written;
4. `study write-reading`, which writes each study's `reading.yaml`.

`--studies` is required. With no `--keys`, the command runs the studies that the folder's
`pick.yaml` lists. One `--ceiling-usd` covers both passes: bounding spends first, and reading gets
what is left. `--model`, `--units-at-once` and `--off-peak-only` work as they do for
`pass start`. A `--model` role goes to each pass whose kind has that role.

Each pass uses its default task, `seqforge/passes/<kind>/` in the current folder. The command adds
no check of its own. It always writes the folder's own `bounds.yaml` and `reading.yaml`, so it
cannot write the standard set's `job-bounds.yaml` and `job-reading.yaml`. Use steps 1 and 3 with
`--name` for those.

It prints one JSON object: the folder, the ceiling, `spent_usd`, and `steps`, with each step's
verb, its exit code and its own output.

| Exit | When |
| --- | --- |
| 0 | Every step finished. |
| 2 | A setup a step cannot use. A setup refused before the first pass spends nothing. |
| 3 | A pass stopped, such as at the ceiling, a task was held by another process, or the format check rejected a file. |

The first step that does not finish stops the chain, and its exit code is the command's. To
resume, run the same command again from the same folder. Every study already answered replays from
the gateway's cache at no cost. A file already written is kept.

On the 50 studies of the standard set, a first run cost $0.0104 for bounding and $1.537 for
reading, about 3 cents a study. Reading took about 22 minutes at four studies at once. The
[note on the run of 2026-11-27](https://github.com/liuhlab/seqforge/blob/main/docs/research/study-chain-on-the-50-2026-11-27.md)
gives the method and how each study ended.

## After the chain

Two commands use a study folder once the chain has written it:

- `seqforge report --study <folder>/<ID>` writes `report.html` and `report.json` into that study
  folder. It shows the folder's own `bounds.yaml` and `reading.yaml`, never the `job-*.yaml`
  files. It does not show whether the job settled the study: that is kept in the pass's task.
- `seqforge run --study <folder>/<ID>` joins the reading into the compiled `sample_metadata.tsv`
  by BioSample accession, when you compile the study's FASTQ files.
  [From FASTQ files to a pipeline](how-it-works/compiler.md) says more.

## Files that exist today

[Files from the hand work](hand-work-files.md) lists every bounds file, reading and verdict made so
far: where each one is, its format and who made it.
