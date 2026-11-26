# The standard set

The standard set is the 50 small GEO studies that later work is built and checked on: 17 on
aging, 17 on neurodegeneration and 16 on *C. elegans*. A study's **bounds** say which GEO Series
and which papers belong to it. Each study's bounds are in a file of their own, in a
[written format](../../docs/formats/bounds.md) that a command checks. A study's reading has a
[written format](../../docs/formats/reading.md) of its own.

This folder holds:

- [`pick.yaml`](pick.yaml): the 50 picked studies and 14 spares, each with the reason it was picked,
  and `replaced`, the studies taken out after picking with the reason each failed.
- one subfolder for each study, named after the Series it was picked by.

```bash
seqforge standard-set list     # one JSON object per study
```

Each object gives the study's summary, direction, record identifiers, the reason it was picked and
its bounds.

## How the 50 came out

All 50 were drawn on 2026-11-19 and 2026-11-20 (UTC) by the two models below, on store build
`20261107T163933Z`. The checking model agreed with 48 drafts and replaced two. It was unsure about
none, and no study had to be replaced by a spare.

- **GSE101112:** the draft kept a later paper that only re-analyses the data. The full text of that
  paper settled it.
- **GSE126954:** one of its eight samples is a re-deposited sample of an older study, and the
  checking model drew the study as the other seven. A study is whole Series, so its bounds are now
  the whole Series. Telling the re-deposited sample apart is left to the reading of the study.
- **Three studies were picked as possibly two studies in one Series** (GSE101112, GSE131269,
  GSE132886). Each turned out to be one study whose second paper re-uses the data or is linked by
  mistake. `pick.yaml` marks them `shape_not_found`.

What the links between records showed is written up in
[`docs/research/standard-set-record-links-2026-11-19.md`](../../docs/research/standard-set-record-links-2026-11-19.md).

## Draw a study

The bounding job draws a study. It reads
liulab-data's stores, so set `LABDATA_STORE` and `LABDATA_PAPERS`. It also needs the lab gateway:
see [Passes](../../docs/passes.md).

```bash
seqforge pass start study-bounds --ceiling-usd 0.1
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml
```

Without `--keys`, the pass runs every study of the pick. `write-bounds` writes the job's bounds as
`<id>/job-bounds.yaml`, beside the study's own `bounds.yaml`, and never over a file.

To swap a spare into the set, run the pass with `--keys` naming the spare, then `write-bounds` with
no `--name`: that writes the spare's own `<id>/bounds.yaml`. Nothing writes its `study.yaml` now.
Write it by hand from the spare's entry in `pick.yaml`, or `seqforge standard-set list` leaves the
study out.
[The study workflow](../../docs/study-workflow.md#1-bound-the-study) says how the job decides.

The 50 studies' own files came from three commands that are now retired. A pass of the
`standard-set-bounds` kind had `deepseek/deepseek-v4-flash` draft each study, `standard-set draft`
wrote each `draft.yaml`, and `standard-set judge` asked Claude (`claude-fable-5-2`) about one study
and wrote its `verdict.yaml`, `bounds.yaml` and `study.yaml`. Where the judge disagreed, its bounds
became the study's and the draft was kept.

## Read a study

The reading job writes each study's reading from the study's own `bounds.yaml`, with the same
stores and gateway as the bounding job.

```bash
seqforge pass start study-reading --ceiling-usd 2 --units-at-once 2
seqforge study write-reading --out evals/standard-set --name job-reading.yaml
```

`write-reading` writes the job's reading as `<id>/job-reading.yaml`, beside the five worked
examples, and never over a file.
[The study workflow](../../docs/study-workflow.md#3-read-the-study) says how the job decides.

## Look at the studies

A prototype page draws one picture per study, the paper beside its GEO datasets. It is kept on the
tag
[`archive/prototype-standard-set-page`](https://github.com/liuhlab/seqforge/tree/archive/prototype-standard-set-page/prototypes/standard_set_page),
and its README there says how to build it.

## One folder per study

[Files from the hand work](../../docs/hand-work-files.md) lists every file here for someone outside
the project: where it is, its format, who made it, and how to load it.

| File | Written by | Holds |
|---|---|---|
| `<id>/study.yaml` | the pipeline | the study: why it was picked, its records, where its bounds came from |
| `<id>/bounds.yaml` | the pipeline | the study's bounds |
| `<id>/job-bounds.yaml` | the bounding job | its own bounds, once it has run on the set |
| `<id>/job-reading.yaml` | the reading job | its own study reading, once it has run on the set |
| `<id>/draft.yaml` | the small model | the drafted summary and bounds |
| `<id>/verdict.yaml` | the large model | whether it agrees with the draft, and why |
| `<id>/reading.yaml` | a large model | the study reading, for five worked examples only |
| `<id>/reading.verdict.yaml` | a second pass of a large model | whether it agrees with the reading, each disagreement, and what it could not settle |
| `<id>/reading.notes.md` | the model that wrote the reading | what the format could not hold for this study |

Five studies have a worked reading, one per kind of design: GSE100021 (two groups), GSE112068 (a
dose and time series), GSE235532 (pull-down libraries with their input controls), GSE117724 (two
assays) and GSE241718 (one paper over two Series). A model wrote each and a second model pass
checked it against the records. No person has checked them. Where the second pass disagrees, the
reading is left as written and the disagreement is in the verdict. The format is on the
[study reading](../../docs/formats/reading.md) page. A prototype page to look at them is kept on the
tag
[`archive/prototype-study-reading-page`](https://github.com/liuhlab/seqforge/tree/archive/prototype-study-reading-page/prototypes/study_reading_page).

No file is overwritten after it is written, with one exception. Each of the 50 `study.yaml` files
was rewritten once to move its bounds out into `bounds.yaml`, so that the bounds
exist in one place. The archived pages were view-only: a correction file is written only when a
person actually corrects a study.

## `bounds.yaml`

[The bounds file](../../docs/formats/bounds.md) describes every field. In short: the Series that
belong to the study, its papers, each linked Series or paper left out with a reason, and one
reason line. A study is whole Series, so no field names samples.

```bash
seqforge study check-bounds evals/standard-set/<id>/bounds.yaml
```

The command exits 0 when the file is in the current format, and 3 with each problem named when it
is not. All 50 files pass. They were converted from the bounds the two models drew, which came
with no quotes and no probabilities. So each claim in them is marked `inferred`, with the model's
reason as its text, and each `p` is `null`: no model has given one yet.

## `study.yaml`

| Field | Meaning |
|---|---|
| `id` | The folder name: the Series the study was picked by. |
| `summary` | One short plain line saying what the study is. From the draft. |
| `direction` | `aging`, `neurodegeneration` or `c_elegans`. A study counts under one. |
| `also_fits` | Other directions it could count under. May be empty. |
| `hard_shape` | `recites_older_series`, `paper_over_several_series`, `superseries`, `series_with_two_studies`, `series_without_paper`, or `null` for the plain case. The shape the study was picked for; the bounds say what it turned out to be. |
| `reason_picked` | One or two sentences. |
| `store_build` | The liulab-data store build the identifiers were read from. |
| `records` | Every linked record, by identifier only: `series`, `superseries` (which of those Series is the SuperSeries, or `null`), `bioprojects`, `pmids`. No record text. |
| `bounds_from` | `draft` when the judge agreed, `judge` when its version replaced the draft. |
| `status` | `settled`, or `unsure` when the judge was unsure. |
| `read_to_decide` | What had to be read: `records`, `abstract`, `full_paper`. |

The pipeline writes the last three together with the verdict.

## The other two files

- **Draft:** `model`, `pass` (the drafting kind, its version and the pass's number), `when`,
  `summary`, `bounds`, and `checks_failed`: what the code found malformed in the draft, if anything.
  A malformed draft is still judged, and the judge then has to give its own bounds.
- **Verdict:** `model`, `when`, `agrees`, `bounds` (the judge's own, only when it disagrees),
  `reason`, `unsure`, `read`.

In these two files `bounds` is what the model answered, not the bounds format: `series`, `papers`,
one `reason`, and `left_out` as a reason for each identifier. The 50 drafts and the two verdicts
with bounds of their own also hold `split_samples`, from when a Series could be split between two
studies. It is empty in all but the verdict of GSE126954. These files are kept as they were
written.
