# The standard set

**Provisional.** The standard set is the 50 small GEO studies that later work is built and checked
on: 17 on aging, 17 on neurodegeneration and 16 on *C. elegans*. A study's **bounds** say which GEO
Series and which papers belong to it. The file format can still change.

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
- **GSE126954:** one of its eight samples is a re-deposited sample of an older study, so the study
  is seven samples of the Series. This is the one study with a sample split.
- **Three studies were picked as possibly two studies in one Series** (GSE101112, GSE131269,
  GSE132886). Each turned out to be one study whose second paper re-uses the data or is linked by
  mistake. `pick.yaml` marks them `shape_not_found`.

What the links between records showed is written up in
[`docs/research/standard-set-record-links-2026-11-19.md`](../../docs/research/standard-set-record-links-2026-11-19.md).

## Draw a study

A small model drafts the bounds, and a large model judges the draft once. Both read liulab-data's
stores, so set `LABDATA_STORE` and `LABDATA_PAPERS` to the store that holds the picked studies. The
drafting pass also needs the lab gateway and a clone of the review repo: see
[Passes and reviews](../../docs/passes-and-reviews.md).

```bash
seqforge pass start standard-set-bounds --ceiling-usd 0.5 --keys KEYS.txt
seqforge standard-set draft
seqforge standard-set judge <id>
```

1. **The drafting pass** runs `deepseek/deepseek-v4-flash` over the studies in `KEYS.txt`, one id a
   line. Without `--keys` it runs every study of the pick. The model reads the records of the
   linked group and each paper's abstract, nothing else.
2. **`draft`** writes `<id>/draft.yaml` from the pass's answers, for each study that has none.
3. **`judge`** asks Claude (`claude -p`, model `claude-fable-5-2`) about one study. It gets the
   draft and what the drafting model saw, and may open the full records and the full paper texts.
   It writes `<id>/verdict.yaml` and `<id>/study.yaml`. Judge one study at a time. If the judge
   refuses a study, the command exits 3 and writes nothing: report it, and do not rephrase the
   request.

Where the judge disagrees, its bounds become the study's and the draft is kept. A study the judge
is unsure about stays, with `status: unsure`. The judge's reading material holds record and paper
text, so it goes to a work folder that git ignores (`--work`), never into this folder.

To look at the drawn studies, build the reading page:
[`prototypes/standard_set_page/`](../../prototypes/standard_set_page/).

## One folder per study

| File | Written by | Holds |
|---|---|---|
| `<id>/study.yaml` | the pipeline | the study: why it was picked, its records, its bounds |
| `<id>/draft.yaml` | the small model | the drafted summary and bounds |
| `<id>/verdict.yaml` | the large model | whether it agrees with the draft, and why |

No file is overwritten after it is written. The page that shows the studies is view-only: a
correction file is written only when a person actually corrects a study.

## `study.yaml`

| Field | Meaning |
|---|---|
| `id` | The folder name: the Series the study was picked by. Two studies cut from one Series add `-a` and `-b`. |
| `summary` | One short plain line saying what the study is. From the draft. |
| `direction` | `aging`, `neurodegeneration` or `c_elegans`. A study counts under one. |
| `also_fits` | Other directions it could count under. May be empty. |
| `hard_shape` | `recites_older_series`, `paper_over_several_series`, `superseries`, `series_with_two_studies`, `series_without_paper`, or `null` for the plain case. The shape the study was picked for; the bounds say what it turned out to be. |
| `reason_picked` | One or two sentences. |
| `store_build` | The liulab-data store build the identifiers were read from. |
| `records` | Every linked record, by identifier only: `series`, `superseries` (which of those Series is the SuperSeries, or `null`), `bioprojects`, `pmids`. No record text. |
| `bounds` | What belongs to the study. See below. |
| `bounds_from` | `draft` when the judge agreed, `judge` when its version replaced the draft. |
| `status` | `settled`, or `unsure` when the judge was unsure. |
| `read_to_decide` | What had to be read: `records`, `abstract`, `full_paper`. |

The pipeline writes the last three together with the verdict.

`bounds` holds:

- `series`: the Series that belong to the study. For a SuperSeries these are its sub-Series.
- `papers`: paper identifiers, written `pmid:<number>`.
- `reason`: one line.
- `left_out`: each linked Series or paper that is not in the study, with a reason of about
  eight words at most.
- `split_samples`: a list of samples per Series, only where a Series is split between two
  studies. Empty otherwise.

## The other two files

- **Draft:** `model`, `pass` (the drafting kind, its version and the pass's number), `when`,
  `summary`, `bounds`, and `checks_failed`: what the code found malformed in the draft, if anything.
  A malformed draft is still judged, and the judge then has to give its own bounds.
- **Verdict:** `model`, `when`, `agrees`, `bounds` (the judge's own, only when it disagrees),
  `reason`, `unsure`, `read`.
