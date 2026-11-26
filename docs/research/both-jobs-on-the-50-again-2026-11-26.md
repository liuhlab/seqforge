# Both jobs on the 50 studies again

Dated 2026-11-26, for [#681](https://github.com/liuhlab/seqforge/issues/681) of the architecture
review [#656](https://github.com/liuhlab/seqforge/issues/656), on the toy store of the standard set,
build `20261107T163933Z-subset`. **The bounding job paid $0.0000 and wrote 50 files that are byte
for byte the files of 2026-11-21: 47 settled, 3 unsettled, none refused. The reading job paid
$0.0512 for 787 new requests and wrote 50 files from reading kind `2026.11.9`: 38 settled, 12
unsettled, none refused. One study's ending moved, GSE288106 from settled to unsettled. Three
studies' files changed in content: GSE126954, GSE142633 and GSE147401. Every other file changed
only in the line that names the ontology snapshots. All 100 files pass their check.**

The question: after the review changed both jobs, what does each job write for the 50 studies, what
does that cost, and which studies' endings or files moved, and why?

Evidence class: everything here is **measured** in one pass of each job on one laptop on
2026-11-26, unless a line says **inferred**. The comparison is with the files of
[the bounding job on the 50](bounding-job-on-the-50-2026-11-21.md) and
[the reading job on the 50](reading-job-on-the-50-2026-11-24.md). No number here scores a file
against the standard set or against the sources. Scoring is a separate effort.

## Method

The bounding pass ran seqforge `e7b6f5e`. The reading pass ran `ec29412`, which adds one change to
the reader ([#685](https://github.com/liuhlab/seqforge/issues/685)) and nothing else under `src/`.
Both ran liulab-llms 2026.11.9 through the lab gateway on the same release, and liulab-data
`6377bd2`. Each role took the kind's default model: `typesafe/jev-1.14.0` as Jev in both jobs, and
`deepseek/deepseek-flash` with thinking on as the reading job's writer. Bounding ran kind version
`2026.11.2` and reading ran kind version `2026.11.9`. Both ran 4 studies at once, each in a new task
folder. `<scratch>` is a scratch folder, and `<toy>` is the folder that holds the toy store's two
parts:

```bash
export LABLLM_GATEWAY=http://127.0.0.1:8000
export LABDATA_STORE=<toy>/geo-sra-store LABDATA_PAPERS=<toy>/paper-store
export LABDATA_ONTOLOGIES=<scratch>/ontologies
seqforge pass start study-bounds --ceiling-usd 2 --units-at-once 4 \
  --label study-bounds-50-681 --task <scratch>/task-bounds
seqforge pass start study-reading --ceiling-usd 3 --units-at-once 4 \
  --label study-reading-50-681 --task <scratch>/task-reading
rm evals/standard-set/*/job-bounds.yaml evals/standard-set/*/job-reading.yaml
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml --task <scratch>/task-bounds
seqforge study write-reading --out evals/standard-set --name job-reading.yaml --task <scratch>/task-reading
seqforge study check-bounds evals/standard-set/<ID>/job-bounds.yaml     # once per study
seqforge study check-reading evals/standard-set/<ID>/job-reading.yaml   # once per study
```

**The ontology store.** The laptop held no ontology store, so one was made for the run with
liulab-data's `update`. It holds the same 13 vocabularies as the store of 2026-11-24, and 12 of
them at the same snapshot. FBdv's current file was a newer release, so the store took FBdv's
2026-11-02 release from that release's own address. The one snapshot that differs is NCBI
Taxonomy: `ncbitaxon-2026-11-26` in place of `ncbitaxon-2026-11-25`. NCBI names its taxonomy file by
the day it was made and does not keep the daily files. The reading job never searches NCBI
Taxonomy. It looks up only the species id that the record gives.

**What the hash list shows.** A SHA-256 list of every file under `evals/standard-set/`, taken
before and after, differs only in the 50 `job-reading.yaml` files. The 50 `job-bounds.yaml` files
were deleted and written again with the same bytes. The set's own `bounds.yaml` files and the five
worked readings are untouched.

## The bounding job

| Measure | Value |
| --- | --- |
| Paid | $0.0000 |
| Requests sent | 0 |
| Answers from the gateway's cache | 92 |
| Dollars when each answer was first paid | $0.0104 |
| Wall time | 24 seconds |

| Ending | Studies |
| --- | --- |
| Settled | 47 |
| Unsettled | 3 |
| Refused | 0 |

**Nothing moved.** The pass sent Jev no request. Each of the 46 studies that ask Jev took both of
its answers from the cache, so the text of every closed question is the same, byte for byte, as on
2026-11-21. The other four studies, GSE112068, GSE142633, GSE208154 and GSE240375, have no record
to ask about. The three unsettled studies are the same three, and each disagrees on the same paper:
GSE131269 on pmid:32494643, GSE147401 on pmid:29167199 and GSE306935 on pmid:32198156.
`write-bounds` wrote 50 files that are the same bytes as the files they replace, and `check-bounds`
accepts all 50.

The change of [#665](https://github.com/liuhlab/seqforge/issues/665) shows in the task and not in the
file. Each answer now saves Jev's two askings of each question in the form that both jobs share.
The bounds file never held them.

## The reading job

| Measure | Median | 90th percentile | Max | Total |
| --- | --- | --- | --- | --- |
| Dollars paid in this pass | $0.0009 | $0.0018 | $0.0025 (GSE310667) | $0.0512 |
| Dollars when each answer was first paid | $0.0301 | $0.0444 | $0.0545 (GSE310667) | $1.5370 |
| Requests sent | 15.5 | 24 | 32 (GSE101112) | 787, and 987 answers from the cache |
| Seconds | 3.8 | 7.7 | 16.9 (GSE60340) | 241, summed over studies run 4 at once |

The pass took 69 seconds of wall time. Nearly every answer came from the cache, so the 22 minutes of
a read from scratch did not apply.

| Ending | Studies | File |
| --- | --- | --- |
| Settled | 38 | Written, and accepted by `check-reading` |
| Unsettled | 12 | Written, and accepted by `check-reading` |
| Refused | 0 | None |

**Where the 787 requests come from.** The 50 files hold 787 samples, and each sample's second
asking was a new request. In reading kind `2026.11.9` ([#685](https://github.com/liuhlab/seqforge/issues/685)),
a sample's second asking, with the options reversed, holds only the questions about the sample's
level on each factor. Its value on each label is asked once, in the first asking. The first asking
is the same text as before, and it came from the cache. So did every other request: the writer's,
each factor's, each passage check, each tie and each term pick. A first read of the 50 now costs
$1.537. That is $0.030 less than the $1.567 of 2026-11-24, because the second askings are shorter
(**inferred** from the two first-paid totals).

**What did not move.** Each of the 50 writer answers was used the first time, as on 2026-11-24.
The writer's replies came from the cache, so the stricter reply reader of
[#669](https://github.com/liuhlab/seqforge/issues/669) took every one of them. The pass checked
927 quotes, did not find 11, and made 299 searches: the totals of 2026-11-24. The pull-down guide
went to the same 5 studies. No study ended unsettled because Jev left a question unanswered. The
label table ([#674](https://github.com/liuhlab/seqforge/issues/674)), the three new check rules
([#675](https://github.com/liuhlab/seqforge/issues/675)), the direct calls to the ontology store
([#676](https://github.com/liuhlab/seqforge/issues/676)) and the bounds file read through its check
([#673](https://github.com/liuhlab/seqforge/issues/673)) changed no file and no ending.

### Endings that moved

| Study | 2026-11-24 | 2026-11-26 | Where Jev's two askings disagree now | Cause |
| --- | --- | --- | --- | --- |
| GSE288106 | Settled | Unsettled | GSM8759027: its level of `disease`. `TDP_A` at 0.52 in the first asking, `TDP_A_C9` at 0.47 in the second | The second asking's new text ([#685](https://github.com/liuhlab/seqforge/issues/685)) |

GSE288106's file did not change beyond its snapshot line. The ending comes from the two askings,
and the file is written from the first.

One unsettled study stayed unsettled on another sample. GSE97461 disagreed on GSM2565655's level of
`treatment`. Now GSM2565655 agrees (`na` at 0.60, then `na` at 0.38), and GSM2565660 disagrees
(`na` at 0.53, then `unknown` at 0.40). The cause is the same second asking. The other 10 unsettled
studies disagree on exactly the questions that the note of 2026-11-24 lists.

### Files whose content changed

Every one of the 50 files names `ncbitaxon-2026-11-26` in its `ontology_snapshot`, and its `date` is
2026-11-26, the UTC day the pass pinned the store's snapshots. 47 files changed only there. Three
changed in their claims, each because of one threshold
([#666](https://github.com/liuhlab/seqforge/issues/666)): a question asked once is yes only above
0.5, and at exactly 0.5 it is neither. The reader of 2026-11-24 read 0.5 as yes.

| Study | What changed | Jev's p |
| --- | --- | --- |
| GSE126954 | Factor `development_stage`: `ordered` went from `true` to `false` | 0.5, **inferred** |
| GSE142633 | `disease` (`healthy`) on 7 of its 13 samples: passage 8, "wild type (WT)", no longer backs the claim, so each claim went from `quoted` to `inferred`, with the passage under `see` | 0.5, **inferred** |
| GSE147401 | The tie between GSM4430047 and GSM4430053 is no longer kept, so each has a biosample of its own with no text that ties it. The biosample ids of the six samples after them in the file each went up by one | 0.5, written in the file of 2026-11-24 |

The two **inferred** values follow from the code. Each request came from the cache, so Jev's reply
is the reply of 2026-11-24. The old reader read p ≥ 0.5 as yes and the new one reads p > 0.5 as
yes, so only a p of exactly 0.5 changes the result. A reply the new reader could not take would
have ended the study unsettled, and neither study failed that check.

## What this does and does not show

- Every number here is measured from the two tasks and the 100 files, except the lines marked
  **inferred**. Nothing here says whether a reading is right.
- The bounding job and all but the second askings of the reading job replayed answers from the
  cache. They show that the review's changes kept the text sent to the models the same. They do
  not show what a read from scratch costs today, beyond the first-paid totals.
- The ontology store differs from the store of 2026-11-24 in its NCBI Taxonomy snapshot only. That
  changed every file's snapshot line and nothing else in them.
