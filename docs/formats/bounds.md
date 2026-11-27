# The bounds file

A **study** is one piece of research: the experiments one paper reports, or one project that has
no paper yet. Public archives do not store studies. GEO stores **Series**, and a Series links to
papers and to other Series. Not everything linked belongs to the same study.

A study's **bounds** say what belongs to it: which GEO Series, and which papers. The bounds file
writes that down, one file per study, as YAML. This page is all you need to read one.

```bash
seqforge study check-bounds evals/standard-set/GSE101112/bounds.yaml
```

The 50 studies of the standard set each have one, at `evals/standard-set/<ID>/bounds.yaml`.

## An example

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

This example is made up to show every part. Its quotes, numbers and model names are not real.

## The fields

Every field below is required, except `titles`. A file with any other field is rejected.

| Field | Holds |
| --- | --- |
| `format` | The whole number `1`. See [When the format changes](#when-the-format-changes). |
| `study` | The study's name: the GEO Series it was picked by, such as `GSE101112`. |
| `models` | Who wrote the file. `bounds` is the model that drew the bounds. `probabilities` is the model that gave the `p` values. Either may be `null`. |
| `series` | The Series that belong to the study. At least one. Each is a claim, and its `id` is written `GSE<number>`. |
| `papers` | The papers that report the study, each written `pmid:<number>`. May be empty. |
| `left_out` | Each linked Series or paper that does not belong. Each is a claim. |
| `reason` | One line saying why these Series and papers are one study. Cannot be blank. |
| `passages` | The quotes that back the claims, each under its number. May be empty: `{}`. |
| `titles` | Optional. Each record's title, under the record's id, copied from its GEO record or its paper. A record with no title has no entry. Each id must be a record the file places, and a title cannot be blank. |

**Titles are copied, not claimed.** The bounding job copies them from the records it read, so that
a reader of the file needs no record store. The 50 files of the standard set were drawn before
titles were copied, and have none.

**A study is whole Series.** A Series is in the study or it is left out. No field names some of
a Series' samples. Telling apart the experiments inside one Series is a later step's job, not the
bounds'.

For a SuperSeries, `series` lists its sub-Series, and the SuperSeries itself is in `left_out`. So
the study's name is not always one of its `series`.

A record is placed once. It is in `series`, in `papers` or in `left_out`, never in two of them and
never twice.

## Claims

A **claim** is one decision the file makes: one Series kept, or one record left out. Each entry
of `series` and of `left_out` is a claim. A claim holds the record's `id`, exactly one backing
mark, and `p`.

| Field | Holds |
| --- | --- |
| `id` | The record: a Series such as `GSE101112`, or a paper such as `pmid:33294943`. Cannot be blank. |
| `quoted` | Backing mark. A source says it: the numbers of the passages that say so. |
| `inferred` | Backing mark. No passage says it outright: a short text saying how it was worked out. |
| `unknown` | Backing mark. What was read does not say: a short note. |
| `see` | Optional, and not beside `quoted`: the numbers of the passages the claim rests on. |
| `p` | A number from 0 to 1, or `null`. Required. |

The rules for a claim:

- It has exactly one of `quoted`, `inferred` and `unknown`. None is rejected, and so are two.
- `quoted` lists at least one passage.
- Every number in `quoted` and in `see` must be a passage in the file.
- The text after `inferred` or `unknown` cannot be blank.

In `left_out`, the backing is also the reason the record does not belong. For an `inferred` claim
that is its text. For a `quoted` claim it is what the passages say. There is no separate reason
field.

**`p`** is the chance that the claim as written is right, as a model judged it. `null` means that
no model has given one yet. It does not mean the claim is in doubt. When any claim has a number,
`models.probabilities` must name the model that gave it.

A [reading file](reading.md) writes its claims and its passages the same way, so the two kinds
of file read alike. A claim in a reading has one more backing mark, `na`, which the bounds do not
use, and more fields for what the claim says.

## Passages

A quote is written once in the file, however many claims it backs. `passages` is keyed by number:
a whole number, 1 or more. Each passage holds the quote and where it was read, in one of two ways.

| Field | Holds |
| --- | --- |
| `quote` | The words, copied from the source. |
| `record` and `field` | For a quote from a GEO record: the record, and the field of it. |
| `paper` and `section` | For a quote from a paper: the paper, and the section of it. |

## The 50 files of the standard set

The 50 studies were drawn before quotes and probabilities were part of the format. Their files
were converted, so in each of them:

- every claim is `inferred`, and its text is the reason the model wrote when it drew the study;
- every `p` is `null`, and so is `models.probabilities`;
- `models.bounds` names the model whose bounds became the study's;
- `passages` is empty.

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

It exits 0 for a valid file, 3 for a rejected one, and 2 for a file it cannot read. A file that is
not UTF-8 text cannot be read. Each problem names the field and the reason. `series[0]` is the
first entry of `series`.

A key that one mapping writes twice is rejected, and the problem names the key. Plain YAML keeps
the last of the two and reports nothing, so a repeated field would go unseen.

A value has the type that this page writes for it. A number in quotes, such as `p: "0.5"`, is
rejected, and so is `true` where a number belongs.

The check asks only whether the file is well formed. It does not open GEO, and it does not judge
whether the bounds are right.

## When the format changes

There is one format at a time. `format` is a whole number, and the check accepts only the current
one. A file with another number is rejected, and the message names the number the check expected.

A change to the format raises the number and rewrites every existing file in the same change. No
file stays behind in an older format, and git history is the only record of the old form.

`titles` came in without a new number. Every file without it stays valid, so no file had to be
rewritten.
