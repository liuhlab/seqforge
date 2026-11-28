# Files from the hand work

This page lists every file in the standard set's folder. It is for someone outside the project who
builds an evaluation on these files. For each kind of file, it gives the path, the count, the
format, who made the file and how, and a few lines of Python that load it.

## Five words this page uses

- A **study** is one piece of research: the experiments one paper reports, or one project that has
  no paper yet.
- A **Series** is one deposit in GEO, the Gene Expression Omnibus, a public archive of sequencing
  data. A Series has an id such as `GSE241718`. One study can span several Series.
- A study's **bounds** say which Series and which papers belong to the study.
- The **standard set** is a fixed list of 50 small GEO studies.
- A study's **reading** says what each sequenced sample is and which labels the study varied.

**Hand work** means work done one study at a time, outside the unattended chain that
[the study workflow](study-workflow.md) describes. The name does not mean that a person did it.
A model made every file on this page, or code wrote the file from a model's answer. No person has
checked any of them.

## Every file in one table

All files are under `evals/standard-set/` in the
[seqforge repository](https://github.com/liuhlab/seqforge/tree/main/evals/standard-set). `<ID>` is
the Series that a study was picked by. It is also the name of the study's folder. The counts were
measured on 2026-11-27.

| File | Path | Count | Made by | Format |
| --- | --- | --- | --- | --- |
| The pick | `pick.yaml` | 1 | A model session, 2026-11-19 | [On this page](#pickyaml-the-50-picked-the-spares-and-the-replaced) |
| The study file | `<ID>/study.yaml` | 50 | The `judge` command | [On this page](#studyyaml-the-facts-about-one-pick) |
| The bounds | `<ID>/bounds.yaml` | 50 | Code, from the two models' answers | [The bounds file](formats/bounds.md) |
| The job's bounds | `<ID>/job-bounds.yaml` | 50 | The bounding job, `typesafe/jev-1.14.0`, 2026-11-27 | [The bounds file](formats/bounds.md) |
| The job's reading | `<ID>/job-reading.yaml` | 50 | The reading job, `deepseek/deepseek-flash` and `typesafe/jev-1.14.0`, 2026-11-27 | [The reading file](formats/reading.md) |
| The draft | `<ID>/draft.yaml` | 50 | `deepseek/deepseek-v4-flash` | [On this page](#draftyaml-what-the-small-model-answered) |
| The verdict | `<ID>/verdict.yaml` | 50 | `claude-fable-5-2` | [On this page](#verdictyaml-what-the-judge-answered) |
| The reading | `<ID>/reading.yaml` | 5 | `claude-fable-5-2` | [The reading file](formats/reading.md) |
| The reading's verdict | `<ID>/reading.verdict.yaml` | 5 | `claude-fable-5-2`, in a second pass | [On this page](#readingverdictyaml-what-the-second-pass-found) |
| The reading's notes | `<ID>/reading.notes.md` | 5 | The model that wrote the reading | Markdown text |
| A person's answers | none yet | 0 | Nobody yet | [The rule](#answers-a-person-gave-while-checking) |

That is 316 files: the pick, six files for each of the 50 studies, and three more for each of the
five studies that have a worked reading. The folder also holds a `README.md`, which describes the
commands.

This snippet counts the files again. Run it, and every other snippet on this page, from the root
of the repository. Each snippet needs Python 3 and PyYAML and nothing else.

```python
import collections
import pathlib

root = pathlib.Path("evals/standard-set")
print(len([p for p in root.iterdir() if p.is_dir()]))  # 50
print(sorted(collections.Counter(p.name for p in root.glob("*/*")).items()))
```

The files hold identifiers and the models' own words. They hold two kinds of text copied from a
record or a paper:

- short quotes, most of them under `passages` in a reading;
- titles, in the `titles` map of each `job-bounds.yaml`.

The records are public at GEO, and PubMed lists each paper by its number.

## The jobs' files

The two jobs of [the study workflow](study-workflow.md) wrote one file each in every study folder.
They sit beside the set's own `bounds.yaml` and `reading.yaml`, under other names, so neither job
writes over the set's own files.

All 100 files were deleted and written again on 2026-11-27, by one run of both jobs over the 50
studies. The run used seqforge at commit `9209509`, the bounding job's version `2026.11.3`, and the
reading job's version `2026.11.10`. The
[note on that run](https://github.com/liuhlab/seqforge/blob/main/docs/research/study-chain-on-the-50-2026-11-27.md)
gives its cost and how each study ended. No study's ending and no claim moved since the run of
2026-11-26.

A file does not say how its study ended: settled, unsettled or refused. That is kept in the pass's
task, and the note lists it.

### `job-bounds.yaml`: the bounding job's bounds

**Path:** `evals/standard-set/<ID>/job-bounds.yaml`. 50 files.

**Format:** [The bounds file](formats/bounds.md) describes every field. All 50 files say
`format: 1`.

**Made by:** the bounding job. A pass of `seqforge pass start study-bounds` answered each study, and
this command wrote the files:

```bash
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml
```

`models` names `typesafe/jev-1.14.0` in 46 files. In the other 4 it is `null`, because the study's
linked group held no record for Jev to place: GSE112068, GSE142633, GSE208154 and GSE240375.

What these files hold that the set's own bounds do not:

- **A probability.** Each claim that Jev answered has a `p`, and its text gives Jev's two answers.
  A claim that code made, such as the starting Series, has `p: null`.
- **`titles`**, new on 2026-11-27. It maps each record the bounds place, kept or left out, to its
  title, copied from the Series record or the paper. A record with no title has no entry.

```python
import yaml

with open("evals/standard-set/GSE241718/job-bounds.yaml", encoding="utf-8") as handle:
    bounds = yaml.safe_load(handle)
print(bounds["models"], [(claim["id"], claim["p"]) for claim in bounds["series"]])
print(bounds["titles"])
```

### `job-reading.yaml`: the reading job's reading

**Path:** `evals/standard-set/<ID>/job-reading.yaml`. 50 files.

**Format:** [The reading file](formats/reading.md) describes every field. All 50 files say
`format: 2`.

**Made by:** the reading job, from each study's own `bounds.yaml`. A pass of
`seqforge pass start study-reading` answered each study, and this command wrote the files:

```bash
seqforge study write-reading --out evals/standard-set --name job-reading.yaml
```

`models.reading` is `deepseek/deepseek-flash`, and `models.probabilities` is `typesafe/jev-1.14.0`.

The run was on the evening of 2026-11-27 in Boston, but each file's `ontology_snapshot.date` says
`2026-11-28`. That date is the day in UTC (Coordinated Universal Time) when the pass opened the
ontology store, because the store names its snapshots by UTC day.

What these files hold that they did not before:

- **`biosample_accession`**, new on 2026-11-27. A sample's row carries it when the sample's GEO
  record links exactly one BioSample. It is the BioSample's `SAMN` id, copied from the record. It
  is an id, not a claim. 785 of the 787 rows carry one. The two rows without one are in GSE254687.

```python
import yaml

with open("evals/standard-set/GSE241718/job-reading.yaml", encoding="utf-8") as handle:
    reading = yaml.safe_load(handle)
print(reading["ontology_snapshot"])
for sample, row in reading["samples"].items():
    print(sample, row.get("biosample_accession"))
```

## The standard set

The 50 studies were picked on 2026-11-19. A small model then drafted each study's bounds, and a
large model judged each draft once. Three commands did that work, and all three are now retired:

1. A pass of the `standard-set-bounds` kind asked the small model about each study.
2. The `standard-set draft` command wrote `draft.yaml` from the answers.
3. The `standard-set judge` command asked the large model about one study. It wrote
   `verdict.yaml`, `bounds.yaml` and `study.yaml`.

The bounding job now draws a study:
[the study workflow](study-workflow.md#1-bound-the-study) says how. You can load every file here
without liulab-data, the lab's package that stores the GEO records and the papers.

The judge agreed with 48 drafts and replaced 2, those of GSE101112 and GSE126954. It was unsure
about none.

### `pick.yaml`: the 50 picked, the spares and the replaced

**Path:** `evals/standard-set/pick.yaml`. One file.

**Made by:** a model session on 2026-11-19. The file names no model, and its first line marks it
`PROVISIONAL`. Its header says that the picker judged each study's direction, shape and design from
the Series' public GEO record and the PubMed abstract of its paper. The picker read 302 Series
records and 263 abstracts, a count taken from the
[note on record links](https://github.com/liuhlab/seqforge/blob/main/docs/research/standard-set-record-links-2026-11-19.md).

The file has six top-level fields:

| Field | Holds |
| --- | --- |
| `store_build` | The build of liulab-data's store that the counts and the links were read from: `20261107T163933Z`. |
| `picked_on` | The date of the pick: `2026-11-19`. |
| `limits` | The size limits a study had to fit: the most samples, experiments and linked Series, and the fewest sequenced runs (`min_sequenced_runs`). The `hard_shape_` limits are wider and apply to a study picked for a hard shape. |
| `studies` | The 50 picked studies: 17 on aging, 17 on neurodegeneration and 16 on *C. elegans*. |
| `spares` | 14 studies held back to take the place of a picked study that fails. |
| `replaced` | The studies taken out after the pick, each with the spare that took its place and the reason it failed. The list is empty: no study has been replaced. |

Each entry of `studies` and of `spares` has these fields:

| Field | Holds |
| --- | --- |
| `id` | The Series the study was picked by. |
| `direction` | `aging`, `neurodegeneration` or `c_elegans`. A study counts under one. |
| `also_fits` | The other directions the study could count under. May be empty. |
| `hard_shape` | The hard case the study was picked for: `recites_older_series`, `paper_over_several_series`, `superseries`, `series_with_two_studies` or `series_without_paper`. `null` for a plain study. 34 of the 50 are `null`. |
| `linked_series` | Every Series linked to the picked one, the picked one included. |
| `pmids` | Every paper linked to any of those Series, each a PubMed number written as text. |
| `assay` | The kind of sequencing, in the picker's short words. |
| `compiles_today` | The library chemistry that the record's protocol text points to, by the name seqforge gives it. A list when the study has two, and `null` when seqforge supports none. No sequencing read was looked at. |
| `compiles_fully` | `true` when every library of the study is in a chemistry that `compiles_today` names. |
| `known_dataset` | `true` for a study that seqforge had already worked on before the pick. 5 of the 50. |
| `n_samples`, `n_experiments` | The GEO samples and the SRA experiments of all the linked Series together. SRA is the Sequence Read Archive, which holds the reads. |
| `design` | The design, in the picker's short words. |
| `reason_picked` | One or two sentences. |
| `status` | `settled`, `shape_to_confirm` or `shape_not_found`. The last one marks a study picked as possibly two studies in one Series that turned out to be one study. 3 of the 50. |
| `spare_for` | Spares only. The direction or the hard shape that the spare stands in for. |

```python
import yaml

with open("evals/standard-set/pick.yaml", encoding="utf-8") as handle:
    pick = yaml.safe_load(handle)
print(len(pick["studies"]), len(pick["spares"]), len(pick["replaced"]))  # 50 14 0
print(pick["studies"][0]["id"], pick["studies"][0]["reason_picked"])
```

### `study.yaml`: the facts about one pick

**Path:** `evals/standard-set/<ID>/study.yaml`. 50 files.

**Made by:** the retired `standard-set judge` command, when it wrote the verdict. It copies the
pick's facts, takes `summary` from the draft, and adds three fields from the verdict. Each file
was rewritten once, on 2026-11-20 (UTC), to move the bounds out into `bounds.yaml`.

| Field | Holds |
| --- | --- |
| `id` | The folder name: the Series the study was picked by. |
| `summary` | One short line that says what the study is. From the draft. |
| `direction`, `also_fits`, `hard_shape`, `reason_picked` | As in `pick.yaml`. |
| `store_build` | The store build the identifiers were read from. All 50 files say `20261107T163933Z-subset`. The subset is a small copy of the build that `pick.yaml` names, cut down to the records of these studies. |
| `records` | Every linked record, by identifier only: `series`, `superseries` (which of those Series is a SuperSeries, or `null`), `bioprojects` and `pmids`. |
| `bounds_from` | `draft` when the judge agreed with the draft (48 files), `judge` when the judge's bounds replaced it (2 files). |
| `status` | `settled`, or `unsure` when the judge was unsure. All 50 say `settled`. |
| `read_to_decide` | What the judge had to read: `records`, `abstract`, `full_paper`. |

```python
import yaml

with open("evals/standard-set/GSE241718/study.yaml", encoding="utf-8") as handle:
    study = yaml.safe_load(handle)
print(study["id"], study["direction"], study["records"]["series"], study["bounds_from"])
```

### `bounds.yaml`: the study's bounds

**Path:** `evals/standard-set/<ID>/bounds.yaml`. 50 files.

**Format:** [The bounds file](formats/bounds.md) describes every field. All 50 files say
`format: 1`.

**Made by:** code, which converted the bounds that the two models drew. `models.bounds` names the
model whose bounds became the study's: `deepseek/deepseek-v4-flash` in 48 files and
`claude-fable-5-2` in 2. The files hold no date. Git history dates the conversion to 2026-11-20
(UTC).

The 50 files hold 74 claims: 57 Series kept and 17 records left out, in 10 studies. Seven studies
have two Series. Four studies have no paper. In three studies the folder's Series is a SuperSeries,
so it is in `left_out` and not in `series`: GSE117724, GSE140511 and GSE240375.

```python
import pathlib

import yaml

for path in sorted(pathlib.Path("evals/standard-set").glob("*/bounds.yaml")):
    bounds = yaml.safe_load(path.read_text(encoding="utf-8"))
    kept = [claim["id"] for claim in bounds["series"]]
    left_out = [claim["id"] for claim in bounds["left_out"]]
    print(bounds["study"], kept, bounds["papers"], left_out)
```

### `draft.yaml`: what the small model answered

**Path:** `evals/standard-set/<ID>/draft.yaml`. 50 files.

**Made by:** `deepseek/deepseek-v4-flash`, through the drafting pass and the `draft` command. The
model read the records of the linked Series and the abstract of each paper, and nothing else. The
drafts are dated from 2026-11-19 23:38 to 2026-11-20 00:05 (UTC).

| Field | Holds |
| --- | --- |
| `id` | The study. |
| `model` | The model that answered. |
| `pass` | The pass that asked: its kind, its version and its number, such as `standard-set-bounds/2026.11.3/pass-5`. The 50 drafts come from four passes. |
| `when` | The time of the answer, as text: `2026-11-20T00:02:16Z`. |
| `summary` | One line that says what the study is. |
| `bounds` | The model's answer. See below. |
| `checks_failed` | What the code found malformed in the answer. Empty in 49 drafts. The draft of GSE126954 put a sample id among the left-out records. |

`bounds` in a draft is the model's answer. It is not in the bounds format:

| Field | Holds |
| --- | --- |
| `series` | The Series the model kept. |
| `papers` | The papers the model kept, each written `pmid:<number>`. |
| `reason` | One line. |
| `left_out` | A mapping from each record left out to the reason. In `bounds.yaml` the same thing is a list of claims. |
| `split_samples` | An older field. See [What this page promises](#what-this-page-promises). |

```python
import yaml

with open("evals/standard-set/GSE241718/draft.yaml", encoding="utf-8") as handle:
    draft = yaml.safe_load(handle)
print(draft["model"], draft["pass"], draft["when"])
print(draft["bounds"]["series"], draft["bounds"]["papers"], draft["bounds"]["left_out"])
```

### `verdict.yaml`: what the judge answered

**Path:** `evals/standard-set/<ID>/verdict.yaml`. 50 files.

**Made by:** `claude-fable-5-2`, through the `judge` command, once for each study. The judge got
the draft and what the drafting model saw. It could also open the full records and the full text
of the papers. The verdicts are dated from 2026-11-19 23:38 to 2026-11-20 00:10 (UTC).

| Field | Holds |
| --- | --- |
| `id` | The study. |
| `model` | The model that judged. |
| `when` | The time of the verdict, as text. |
| `agrees` | `true` when the judge agrees with the draft's bounds. 48 are `true` and 2 are `false`. |
| `bounds` | `null` when the judge agrees. Otherwise the judge's own bounds, with the same five fields as the bounds in a draft. |
| `reason` | Why the judge agrees or disagrees. |
| `unsure` | `true` when the judge could not decide. All 50 are `false`. |
| `read` | What the judge read: `records`, `abstract`, `full_paper`. The judge opened a full paper for 7 studies. |

The bounds that the two models settled on are the draft's when the judge agrees, and the judge's
when it does not:

```python
import pathlib

import yaml

for folder in sorted(pathlib.Path("evals/standard-set").glob("GSE*")):
    draft = yaml.safe_load((folder / "draft.yaml").read_text(encoding="utf-8"))
    verdict = yaml.safe_load((folder / "verdict.yaml").read_text(encoding="utf-8"))
    answer = draft["bounds"] if verdict["agrees"] else verdict["bounds"]
    print(verdict["id"], verdict["agrees"], answer["series"], sorted(answer["left_out"]))
```

## The five worked readings

Five of the 50 studies have a reading of their own, one for each kind of design. **A model wrote
each reading. A second model pass checked each reading against the records and the paper. No
person has checked any of them.** Where the second pass disagrees with a reading, the reading is
left as written, and the disagreement is in the verdict.

| Study | Kind of design | Samples | Passages | Second pass agrees | Disagreements |
| --- | --- | --- | --- | --- | --- |
| GSE100021 | Two groups: a treatment against a control | 8 | 29 | No | 1 |
| GSE112068 | A dose series and a time series | 23 | 45 | No | 3 |
| GSE235532 | Pull-down libraries with their input controls | 24 | 30 | Yes | 1 |
| GSE117724 | Two assays: 18 RNA-Seq libraries and 2 ChIP-Seq libraries | 20 | 54 | Yes | 0 |
| GSE241718 | One paper over two Series | 8 | 28 | Yes | 2 |

No command made these files. The process was:

1. A large model read the records of the study's Series and its paper. It wrote `reading.yaml`
   and `reading.notes.md`. For four studies it read the records, the abstract and the full text.
   GSE112068 has no paper, so the model read the records only.
2. A second pass read the same sources again and checked the reading. It looked for every quote
   in the field or the paper section that the reading names. It compared the samples with the
   study's bounds. It wrote `reading.verdict.yaml`.
3. The second pass changed a reading only to make it pass the format check. Each such change is
   listed under `format_fixes` in the verdict: 7 changes over the five readings.

Each file records the model as `claude-fable-5-2`, for the reading, for its probabilities and for
the second pass.

### `reading.yaml`: the study reading

**Path:** `evals/standard-set/<ID>/reading.yaml`. 5 files.

**Format:** [The reading file](formats/reading.md) describes every field. All five files say
`format: 2`. They were written in format 1 and rewritten to format 2 on 2026-11-21. That change
added the assay's ontology id, the source of the ids, and the passages that speak against a claim.
No claim's text, number, level or `p` changed. Each `reading.notes.md` ends with what changed in it.

**Made by:** `claude-fable-5-2`, named in `models.reading` and in `models.probabilities`. The
files do not say when they were written. They were added to the repository on 2026-11-20 (UTC).
Their `ontology_snapshot.date` is `2026-11-21`, the day of the rewrite to format 2.

These five files carry no `biosample_accession` on any row. The job's `job-reading.yaml` beside
each one does.

A sample has every claim under `shared`, except a label that its own row writes. The snippet
applies that rule:

```python
import yaml

with open("evals/standard-set/GSE241718/reading.yaml", encoding="utf-8") as handle:
    reading = yaml.safe_load(handle)
for sample, row in reading["samples"].items():
    claims = {**reading["shared"], **row}  # a row's claim replaces the shared claim whole
    print(sample, claims["tissue"]["text"], claims["age"]["level"], claims["assay"]["p"])
print(reading["passages"][14]["quote"])  # a passage number loads as a whole number
```

The relations between the samples are not stored. `seqforge study relations <file>` works them
out, and [the reading file](formats/reading.md) describes its output.

### `reading.verdict.yaml`: what the second pass found

**Path:** `evals/standard-set/<ID>/reading.verdict.yaml`. 5 files.

**Made by:** `claude-fable-5-2`, in a second pass. The verdicts are dated from 2026-11-20 01:39 to
02:05 (UTC).

| Field | Holds |
| --- | --- |
| `id` | The study. |
| `model` | The model of the second pass. |
| `when` | The time of the verdict. Four files write it as text. `GSE100021/reading.verdict.yaml` writes it without quotes, so PyYAML returns a `datetime` for that one file. |
| `read` | What the second pass read: `records`, `abstract`, `full_text`. |
| `format_fixes` | A list of text. Each item is one change that the second pass made to `reading.yaml` so that the file passes the format check. |
| `quotes` | Three counts of the quotes in the reading: `checked`, `found`, and `not_found`, a list. In all five, every quote was found: 186 quotes together. |
| `samples_match_bounds` | `true` when the samples of the reading are exactly the samples of the Series in the study's bounds. `true` in all five. |
| `agrees` | The second pass's overall judgement of the reading. `true` in 3 and `false` in 2. A verdict can agree and still list disagreements. |
| `disagreements` | A list, one item for each point where the second pass would write the reading differently. May be empty. |
| `unsure` | A list of text: what the second pass could not settle from the sources. Each verdict has 3 to 6. |
| `reason` | The second pass's account of what it checked and found. |

Each item of `disagreements` has five fields:

| Field | Holds |
| --- | --- |
| `where` | The place in `reading.yaml`, such as `shared.dose` or `samples.GSM7720368.donor`. One item can name several places. |
| `says` | What the reading says there. |
| `should_be` | What the second pass would write. |
| `why` | The reason. |
| `passages` | The passages that the point rests on, by number. One item in the verdict of GSE241718 also quotes a record line as text. |

```python
import pathlib

import yaml

for path in sorted(pathlib.Path("evals/standard-set").glob("*/reading.verdict.yaml")):
    verdict = yaml.safe_load(path.read_text(encoding="utf-8"))
    print(verdict["id"], verdict["agrees"], verdict["quotes"]["found"], len(verdict["unsure"]))
    for item in verdict["disagreements"]:
        print("  ", item["where"], "->", item["should_be"])
```

### `reading.notes.md`: what the format could not hold

**Path:** `evals/standard-set/<ID>/reading.notes.md`. 5 files.

**Made by:** the model that wrote the reading, at the same time. Each file is free Markdown text
with no fixed fields. It says what the model read. It also says what the reading format could not
hold for this study, or held badly.

```python
import pathlib

for path in sorted(pathlib.Path("evals/standard-set").glob("*/reading.notes.md")):
    notes = path.read_text(encoding="utf-8")
    print(path.parent.name, notes.splitlines()[0])
```

## Answers a person gave while checking

There are none yet. No person has answered a question about any of the 50 studies, and no person
has corrected a file. Every study folder holds only the files in the table at the top.

The rule for the first one is fixed. An answer that a person gives while checking is saved as a
small plain file. The file holds the question, the study, the answer, who gave it and when. No
folder and no command exist for such files yet. When the first answer is saved, this page lists
its path.

## What this page promises

**Every file listed here is at the listed path, in the current format.** There is one format at a
time. A change to a format rewrites every existing file in the same change, and this page changes
with it. The page does not promise that the content of a file stays the same.

Two kinds of file have a written format and a check:

```bash
seqforge study check-bounds evals/standard-set/<ID>/bounds.yaml
seqforge study check-reading evals/standard-set/<ID>/reading.yaml
```

The reading check needs liulab-data installed, because liulab-data's vocabularies judge each
ontology id. On 2026-11-27, every file that has a check passed it: the 50 `bounds.yaml`, the 50
`job-bounds.yaml`, the five `reading.yaml` and the 50 `job-reading.yaml`.

The other files have no format number, and their fields are the ones this page lists. The drafts
and the verdicts are kept as the models wrote them, so they hold fields from before the bounds
format:

- **`split_samples`** is in the `bounds` of all 50 drafts, and in the `bounds` of the 2 verdicts
  that have bounds of their own. It comes from when one Series could be split between two studies.
  A study is now whole Series, and the bounds format rejects the field. It is empty in all 50
  drafts and in the verdict of GSE101112. In the verdict of GSE126954 it lists seven sample ids.
- **`left_out`** in a draft or a verdict is a mapping from a record to a reason. In `bounds.yaml`
  it is a list of claims.
- **A paper** is written `pmid:<number>` in the bounds, the drafts, the verdicts and the readings.
  In `pick.yaml` and in `study.yaml` it is the bare number, as text.
- **A full paper** is `full_paper` in `study.yaml` and `verdict.yaml`, and `full_text` in the two
  reading files.

## Known wrong or stale facts in the files

Do not build on these lines without reading this list. Each one was checked against the files on
2026-11-27.

1. **`GSE241718/study.yaml` describes the two Series wrongly.** Its `reason_picked` says that one
   Series holds whole cells and one holds nuclei. The protocol on every sample record, the summary
   of both Series and the paper say that all eight libraries are single-nucleus. The paper says
   that the data were uploaded in two stages. The entry in `pick.yaml` has the same sentence, and
   its `assay` and `design` say the same. That entry also names the chemistry as `10x-3p-gex`,
   while the protocol on every sample record names a Singleron kit.
2. **`GSE126954/bounds.yaml` still speaks of seven samples.** Its claim and its `reason` say that
   "the seven embryo samples are its own data". The judge had drawn the study as seven of the
   eight samples. A study is whole Series, so the bounds are now the whole Series of eight. The
   eighth sample is a re-deposited sample of an older study. Telling it apart is left to the
   study's reading. The set has no worked reading of this study. The job's `job-reading.yaml`
   reads all eight samples, and no one has checked it.
3. **The set's own bounds carry no probability and no quote.** All 74 claims in the 50
   `bounds.yaml` files are marked `inferred`, with the model's reason as the text. Every `p` is
   `null`, `models.probabilities` is `null`, and `passages` is empty. `null` means that no model
   has given a probability, not that the claim is in doubt. The job's `job-bounds.yaml` files do
   carry a `p` on each claim that Jev answered.
4. **One `when` is later than its file.** `GSE100021/reading.verdict.yaml` says
   `2026-11-20T02:05:00Z`. The commit that added the file is dated 23 minutes earlier. The model
   wrote the time itself.
5. **The readings keep what the second pass disagrees with.** The table below has one line for
   each reading.

| Reading | What the second pass disagrees with |
| --- | --- |
| GSE100021 | `shared.dose` writes the value `1.7e+12` with the unit `viral_particles_per_side`. The source sentence gives the content of the suspension, not the amount each side received. |
| GSE112068 | `shared.sex` is `na` and should be `unknown`, on all 23 samples. The three untreated samples have `control` as their treatment, where `na` fits. The time factor cites passages that do not list its levels. |
| GSE117724 | Nothing. The second pass lists four points it could not settle, such as the stage of the worms at harvest. |
| GSE235532 | The reading leaves out the time of collection, and its notes say wrongly that the format has no place for it. |
| GSE241718 | The donor ids of the four young libraries are a guess, and their `p` of 0.5 is too high. Passage 19 names two sample records, and all eight hold the quote. |

The second pass also could not settle the age of the four aged libraries of GSE241718. The sample
records say 24 months, and the Series designs and the paper say 26. The reading writes 26 with a
`p` of 0.55.
