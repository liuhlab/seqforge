# The standard set (prototype)

**Prototype.** This folder shows what one study of the standard set looks like on disk. It holds
one study, GSE206603, and its format is provisional. The draft and the verdict here were written
by hand as stand-ins, and each says so in its first line.

The standard set is the 50 small GEO studies that later work is built and checked on. A study's
**bounds** say which GEO Series and which papers belong to it.

```bash
seqforge standard-set list     # one JSON object per study
```

## Four files per study

| File | Written by | Holds |
|---|---|---|
| `<id>.yaml` | the pipeline | the study: why it was picked, its records, its bounds |
| `<id>.draft.yaml` | the small model | the drafted bounds |
| `<id>.verdict.yaml` | the large model | whether it agrees with the draft, and why |
| `<id>.answer.yaml` | a person | the person's mark, filled in on the side |

No file is overwritten after it is written. The answer file starts empty.

## The study file

| Field | Meaning |
|---|---|
| `id` | The Series the study was picked by. It names the four files. |
| `direction` | `aging`, `neurodegeneration` or `c_elegans`. A study counts under one. |
| `also_fits` | Other directions it could count under. May be empty. |
| `hard_shape` | Why its bounds are hard to draw, or `null` for the plain case. See below. |
| `reason_picked` | One or two sentences. |
| `store_build` | Date of the liulab-data store build the identifiers were read from. |
| `records` | Every record in the linked group, by identifier only. No record text. |
| `bounds` | What belongs to the study. See below. |
| `bounds_from` | `draft` when the judge agreed, `judge` when its version replaced the draft. |
| `status` | `settled`, or `unsure` when the judge was unsure. |
| `read_to_decide` | What had to be read: `records`, `abstract`, `full_paper`. |

`hard_shape` is one of `recites_older_series`, `paper_over_several_series`, `superseries`,
`series_with_two_studies`, `series_without_paper`.

`records` holds `series`, `superseries` (which of those Series is the SuperSeries, or `null`),
`bioprojects` and `pmids`. It lists everything that was looked at. `bounds` lists what belongs.

`bounds` holds:

- `series`: the Series that belong to the study. For a SuperSeries these are its sub-Series.
- `papers`: paper identifiers, written `pmid:<number>`.
- `reason`: one line.
- `split_samples`: a list of samples per Series, only where a Series is split between two
  studies. Empty otherwise.

## The other three files

- **Draft:** `model`, `pass` (the drafting pass's identifier), `when`, `bounds`.
- **Verdict:** `model`, `when`, `agrees`, `bounds` (the judge's own, only when it disagrees),
  `reason`, `unsure`, `read`.
- **Answer:** `mark` (`accept`, `fix` or `drop`), `note`, `bounds` (the corrected ones, only when
  the mark is `fix`), `who`, `when`.
