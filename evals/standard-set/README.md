# The standard set (prototype)

**Prototype.** This folder shows what one study of the standard set looks like on disk. It holds
one study, GSE206603, and its format is provisional. The draft and the verdict here were written
by hand as stand-ins, and each says so in its first line.

The standard set is the 50 small GEO studies that later work is built and checked on. A study's
**bounds** say which GEO Series and which papers belong to it.

```bash
seqforge standard-set list     # one JSON object per study
```

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
| `hard_shape` | `recites_older_series`, `paper_over_several_series`, `superseries`, `series_with_two_studies`, `series_without_paper`, or `null` for the plain case. |
| `reason_picked` | One or two sentences. |
| `store_build` | Date of the liulab-data store build the identifiers were read from. |
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

- **Draft:** `model`, `pass` (the drafting pass's identifier), `when`, `summary`, `bounds`.
- **Verdict:** `model`, `when`, `agrees`, `bounds` (the judge's own, only when it disagrees),
  `reason`, `unsure`, `read`.
