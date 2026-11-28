# The bounds file

A **study** is one piece of research: the experiments that one paper reports, or one project that
has no paper yet. Public archives do not store studies. GEO, the Gene Expression Omnibus, stores
**Series**: a Series is one deposit of samples. A Series links to papers and to other Series, and
not everything it links to belongs to the same study.

A study's **bounds** say what belongs to it: which GEO Series, and which papers. The bounds file
writes the bounds down, one file per study, in YAML, a plain-text format of fields and lists.
Every later step reads it. The [reading file](reading.md) is drawn only from the Series and the
papers that the bounds keep.

This page describes every field and every rule of the file. It is all you need to read one.

## A short example

This is the bounds file that the bounding job wrote for the standard-set study GSE131269. The job
kept the Series and one paper, and it left a second linked paper out.

```yaml
format: 1
study: GSE131269
models:
  bounds: typesafe/jev-1.14.0
  probabilities: typesafe/jev-1.14.0
series:
- id: GSE131269
  inferred: 'code: the study starts from GSE131269'
  p: null
papers:
- pmid:32494643
left_out:
- id: pmid:31279658
  inferred: 'Jev''s p(yes) that pmid:31279658 first reports the data of GEO Series GSE131269: 0.10; its
    p(yes) to the reversed question: 0.89'
  p: 0.9
reason: Code kept GEO Series GSE131269; Jev judged each other record kept here to be from the same research.
passages: {}
titles:
  GSE131269: Identification of the m6Am Methyltransferase PCIF1 Reveals the Location and Functions of
    m6Am in the Transcriptome.
  pmid:31279658: Identification of the m6Am Methyltransferase PCIF1 Reveals the Location and Functions
    of m6Am in the Transcriptome.
  pmid:32494643: N6-adenosine methylation of ribosomal RNA affects lipid oxidation and stress resistance.
```

Read it from the top:

- `series` keeps one Series. Code kept it because the study starts from it.
- `papers` keeps one paper, by its PubMed id (PMID).
- `left_out` holds the other linked paper. Jev, a model that answers yes-or-no questions with a
  probability, judged that this paper does not first report the study's data. Its `p` of 0.9 is the
  chance that this judgement is right.
- `titles` gives each record's title, so you can read the file without opening GEO or PubMed.

## Where the files are

| File | What it is |
| --- | --- |
| `<folder>/<ID>/bounds.yaml` | A study's bounds. `seqforge study write-bounds --out <folder>` writes one per study from the bounding pass's answers. `seqforge study run` runs that pass and that write as its first two steps, and calls models through the model gateway. |
| `evals/standard-set/<ID>/bounds.yaml` | The bounds of each of the 50 studies of the standard set. |
| `evals/standard-set/<ID>/job-bounds.yaml` | The bounds that the bounding job drew for each of the same 50 studies, written with `study write-bounds --name job-bounds.yaml`. |

`<ID>` is the study's name, such as `GSE131269`. `study write-bounds` never overwrites a file that
is already there. It writes no file for a study that the job refused, and it writes no text that the
format check rejects.

To check one file, run:

```bash
seqforge study check-bounds evals/standard-set/GSE101112/bounds.yaml
```

The check reads the file alone. It needs no model and no network. See [The check](#the-check).

## The fields

Every field below is required, except `titles`. The check rejects a file with any other field.

| Field | Holds |
| --- | --- |
| `format` | The whole number `1`. See [When the format changes](#when-the-format-changes). |
| `study` | The study's name: the GEO Series it was picked by, such as `GSE101112`. It cannot be empty. |
| `models` | Who wrote the file. `bounds` is the model that drew the bounds. `probabilities` is the model that gave the `p` values. Either may be `null`. |
| `series` | The Series that belong to the study. At least one. Each entry is a [claim](#claims), and its `id` is written `GSE<number>`. |
| `papers` | The papers that report the study, each written `pmid:<number>`. The list may be empty. |
| `left_out` | Each linked Series or paper that does not belong. Each entry is a claim. The list may be empty. |
| `reason` | One line that says why these Series and papers are one study. It cannot be blank. |
| `passages` | The quotes that back the claims, each under its number. It may be empty: `{}`. See [Passages](#passages). |
| `titles` | Optional. The title of each record that the file places, under the record's id. See [Titles](#titles). |

### What belongs, and how a record is placed

**A study is whole Series.** A Series is in the study or it is left out. No field names some of a
Series' samples. Telling apart the experiments inside one Series is the job of a later step, not of
the bounds.

**A SuperSeries is left out.** A SuperSeries is a GEO Series that groups other Series, its
sub-Series. For a study that starts from a SuperSeries, `series` lists the sub-Series, and the
SuperSeries itself goes in `left_out`. So the study's name is not always one of its `series`.

**A record is placed once.** Each Series or paper is in `series`, in `papers` or in `left_out`. It is
never in two of them, and never twice in one. A left-out record's `id` may be a Series or a paper,
and it cannot be blank.

## Claims

A **claim** is one decision that the file makes: one Series kept, or one record left out. Each entry
of `series` and of `left_out` is a claim. A kept paper is not a claim: it is a plain `pmid:` entry
under `papers`.

A claim holds the record's `id`, exactly one backing mark, and `p`. A backing mark says how the
claim is backed.

| Field | Holds |
| --- | --- |
| `id` | The record: a Series such as `GSE101112`, or a paper such as `pmid:33294943`. It cannot be blank. |
| `quoted` | Backing mark. A source says it. The mark lists the numbers of the passages that say so. |
| `inferred` | Backing mark. No passage says it outright. The mark is a short text that says how it was worked out. |
| `unknown` | Backing mark. What was read does not say. The mark is a short note. |
| `see` | Optional, and never beside `quoted`. The numbers of the passages that the claim rests on. |
| `p` | A number from 0 to 1, or `null`. Required. |

The rules for a claim:

- It has exactly one of `quoted`, `inferred` and `unknown`. The check rejects a claim with none, and
  a claim with two.
- `quoted` lists at least one passage.
- Every number in `quoted` and in `see` must be a passage in the file.
- The text after `inferred` or `unknown` cannot be blank.

In `left_out`, the backing mark is also the reason that the record does not belong. For an
`inferred` claim, the reason is its text. For a `quoted` claim, the reason is what its passages
say. There is no separate reason field.

**`p`** is the chance that the claim as written is right, as a model judged it. `null` means that no
model has given one yet. It does not mean that the claim is in doubt. When any claim has a number,
`models.probabilities` must name the model that gave it.

A [reading file](reading.md) writes its claims and its passages the same way, so the two kinds of
file read alike. A claim in a reading has one more backing mark, `na`, and more fields for what the
claim says, such as `against`. A bounds claim takes neither.

## Passages

A quote is written once in the file, however many claims it backs. `passages` is keyed by number,
a whole number of 1 or more. Each passage holds the quote and where it was read, in one of two
ways.

| Field | Holds |
| --- | --- |
| `quote` | The words, copied from the source. It cannot be empty. |
| `record` and `field` | For a quote from a GEO record: the record, and the field of it. |
| `paper` and `section` | For a quote from a paper: the paper, and the section of it. |

A passage has `record` and `field`, or it has `paper` and `section`. The check rejects a passage
with only one of a pair, or with parts of both.

## Titles

`titles` maps each record that the file places to its title. A placed record is any record in
`series`, in `papers` or in `left_out`, so a left-out record has its title too. Each title is
copied from the record's GEO entry or from its paper, so that a reader of the file needs no record
store.

- A record with no title has no entry.
- Each id under `titles` must be a record that the file places.
- A title cannot be blank.

**Titles are copied, not claimed.** A title has no backing mark and no `p`.

## What the bounding job writes

The bounding job draws a study's bounds from its archive records and papers. Its files have this
shape:

- Code keeps the Series that the study starts from, or each sub-Series when the study starts from a
  SuperSeries. Code leaves each SuperSeries out. These claims are `inferred`, with a text that
  starts `code:`, and their `p` is `null`.
- Jev is asked about every other linked Series and every linked paper, twice: once as the question
  is written, and once with the question reversed. A record is in the study when Jev answers yes to
  the question as written. Its claim is `inferred`. The text gives both of Jev's answers, and `p` is
  Jev's probability for the answer it gave.
- `passages` is `{}`.
- `models` names Jev for both roles when Jev was asked anything. When the linked records held
  nothing to ask about, both are `null`.
- `titles` holds the title of each placed record, from its Series record or its paper.

A study ends **unsettled** when Jev's two answers about a record disagree. It ends **refused** when
its linked records hold more than 50 Series, when Jev gives no answer about a record, or when the
file fails the format check. [Passes](../passes.md) and [the study workflow](../study-workflow.md)
say how a run reports each ending.

### The 50 bounds files of the standard set

The 50 `bounds.yaml` files of the standard set were drawn before quotes and probabilities were part
of the format, and they were converted to it. So in each of them:

- every claim is `inferred`, and its text is the reason that the model wrote when it drew the study;
- every `p` is `null`, and so is `models.probabilities`;
- `models.bounds` names the model whose bounds became the study's;
- `passages` is empty;
- there are no `titles`, because the files were drawn before titles were copied.

The 50 `job-bounds.yaml` files beside them are the bounding job's own output, and each has
`titles`.

## An example with every part

This example is made up to show every part of the format. Its quotes, numbers and model names are
not real. It passes the check.

```yaml
format: 1
study: GSE101112
models:
  bounds: example/model-a
  probabilities: example/model-b
series:
- id: GSE101112
  quoted: [1]
  p: 0.97
papers:
- pmid:31742554
left_out:
- id: pmid:33294943
  inferred: Software paper that uses the dataset only as test data
  see: [2]
  p: 0.9
- id: pmid:32884834
  unknown: Nothing that was read says whether it made new data
  p: null
reason: The 2019 paper made and reports the data held in this Series.
passages:
  1:
    paper: pmid:31742554
    section: Data availability
    quote: "RNA-seq data have been deposited in GEO under accession GSE101112"
  2:
    record: GSE101112
    field: Series_citation
    quote: "A tool paper that lists this Series among its examples"
titles:
  GSE101112: An example Series title
  pmid:31742554: An example paper title
```

## The check

`seqforge study check-bounds <file>` reads one file and prints one JSON object:

```json
{
  "file": "bounds.yaml",
  "format": 1,
  "valid": false,
  "problems": [
    {"field": "series[0].quoted", "reason": "passage 7 does not exist"},
    {"field": "split_samples", "reason": "not a field of the format"}
  ]
}
```

`file` is the path you gave. `format` is the format number that the check accepts. Each problem
names the field and the reason. `series[0]` is the first entry of `series`.

| Exit code | Meaning |
| --- | --- |
| 0 | The file is valid. |
| 3 | The file is rejected. `problems` lists why. |
| 2 | The file cannot be read. A file that is not UTF-8 text cannot be read. |

The check also rejects these, each with exit code 3:

- **Text that is not YAML, or YAML that is not a mapping of fields.** The problem's field is
  `(file)`.
- **A wrong or missing `format`.** The check asks about the format number first, and alone. A file
  with another number gets that one problem, which names the number that the check expected.
- **A key that one mapping writes twice.** The problem names the key. Plain YAML keeps the last of
  the two and reports nothing, so a repeated field would go unseen.
- **A value of the wrong type.** A value has the type that this page writes for it. The check
  rejects a number in quotes, such as `p: "0.5"`, and `true` where a number belongs.

The check asks only whether the file is well formed. It does not open GEO, and it does not judge
whether the bounds are right.

## When the format changes

There is one format at a time. `format` is a whole number, and the check accepts only the current
one. A file with another number is rejected, and the message names the number that the check
expected.

A change to the format raises the number and rewrites every existing file in the same change. No
file stays behind in an older format. Git history is the only record of the old form.

`titles` came in without a new number. Every file without it stays valid, so no file had to be
rewritten.
