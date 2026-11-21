# The bounding job on the 50 studies

Dated 2026-11-21, for [#622](https://github.com/liuhlab/seqforge/issues/622), on the toy store of
the standard set, build `20261107T163933Z-subset`. **The job drew all 50 studies: 47 settled, 3
unsettled, none refused, and every file passes the format check. Each file keeps the same Series
and papers, and leaves out the same records, as the standard set's own `bounds.yaml`. The whole
session paid $0.00105. A pass killed after 11 studies lost none of them, and its rerun answered
those 11 at no cost. With one paper's abstract changed in a copy of the store, the rerun paid for
that one study and took the other 49 from the cache.**

The question: does the bounding job, `seqforge pass start study-bounds` and then
`seqforge study write-bounds`, draw the 50 studies in the bounds format, and does a rerun after a
kill or a changed paper pay only for what it has not answered yet?

Evidence class: everything here is **measured** in three passes on one laptop on 2026-11-21. No
number here scores the job against the standard set. Scoring is a separate effort.

## Method

The passes ran seqforge `df2d43a` with liulab-llms 2026.11.2, Jev `typesafe/jev-1.14.0` in the
`jev` role (the kind's default), and kind version `2026.11.1`. They read the toy store of the 50
studies, through the lab gateway, and used a new empty review clone. `<scratch>` is a scratch
folder that holds the task folder and the review clone, and `<toy>` is the folder that holds the
toy store's two parts:

```bash
cd <scratch>
export LABLLM_GATEWAY=http://127.0.0.1:8000
export LABDATA_STORE=<toy>/geo-sra-store LABDATA_PAPERS=<toy>/paper-store
export SEQFORGE_REVIEWS=<scratch>/clone
seqforge pass start study-bounds --ceiling-usd 2        # pass 1, killed; pass 2, the rerun
seqforge pass status study-bounds
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml
seqforge study check-bounds evals/standard-set/<ID>/job-bounds.yaml   # once per study
```

Pass 3 ran the same `pass start` command with both variables pointed at a copy of the toy store.

## How the 50 ended

| Ending | Studies |
| --- | --- |
| Settled | 47 |
| Unsettled | 3 |
| Refused | 0 |

`write-bounds` wrote a `job-bounds.yaml` for all 50 studies, and `check-bounds` accepted all 50.
No group was over the 50-Series limit. The three unsettled studies each have one paper where Jev's
answer as written and its answer to the reversed question disagree:

| Study | Paper | p(yes) as written | p(yes) reversed |
| --- | --- | --- | --- |
| GSE131269 | pmid:32494643 | 0.85 | 0.58 |
| GSE147401 | pmid:29167199 | 0.04 | 0.18 |
| GSE306935 | pmid:32198156 | 0.04 | 0.23 |

In each, the answer as written places the paper where the standard set places it.

Four studies have no record to ask Jev about, so code wrote their bounds with no model call:
GSE112068, GSE142633, GSE208154 and GSE240375.

## Differences from the standard set's bounds

None. For each of the 50 studies, the job's file and the study's `bounds.yaml` name the same
Series as in, the same papers as in, and the same records as left out. The files differ only in
their text: the job's claims give Jev's two probabilities, and its `models` name Jev.

## Cost and time per study

From `pass status`, for pass 2, the pass that ran all 50 to the end. The per-study figures count the
46 studies that called Jev. The maximum comes from the task's per-study record, since
`pass status` gives the 90th percentile and not the maximum.

| Measure | Median | 90th percentile | Max | Total |
| --- | --- | --- | --- | --- |
| Dollars paid in this pass | $0 | $0 | $0.000498 (GSE117724) | $0.000919 |
| Dollars when each answer was first paid | $0.000205 | $0.000340 | $0.000498 (GSE117724) | $0.0104 |
| Seconds | 0.50 | 0.69 | 0.81 (GSE288106) | 23.8, summed over studies run 32 at once |

Pass 2 sent 4 requests and took 88 answers from the gateway's cache. Most answers were already
in the cache from the prototype that tuned the questions, so the paid column shows what reruns
cost here, and the first-paid column what a first run of the 50 costs: about $0.0002 a study.

| Pass | Paid | Requests | Answers from the cache |
| --- | --- | --- | --- |
| 1, killed | $0 | 0 | 18 |
| 2, rerun to the end | $0.000919 | 4 | 88 |
| 3, changed paper | $0.000129 | 2 | 90 |
| All three | $0.00105 | 6 | 196 |

## The kill test

Pass 1 ran in its own process group. A script read the task's record and killed the group with
`SIGKILL` once 11 studies had an outcome. All 11 had settled, and 9 of them had called Jev.

| Read | Before the rerun | After the rerun |
| --- | --- | --- |
| Pass 1 `ended` | `false` | `false` |
| Units with an outcome | 11 | 50 |
| Kind's counts | settled 11 | settled 47, unsettled 3, refused 0, unfinished 0, held 0 |
| Pass 2 `ended` | — | `true` |

In pass 2, each of the 11 studies answered before the kill paid $0 and sent no request. The 9 that
call Jev took both answers from the cache. The only studies paid for in pass 2 were GSE117724 and
GSE140511, which had no outcome when the pass was killed.

## The changed-paper test

GSE100021's group holds one paper, pmid:28645186, and no other study's group holds it. In a copy
of the toy store, one sentence was added to that paper's abstract. Pass 3 ran the same command
against the copy.

- `pass status` for pass 3: 2 requests, $0.000129 paid, 90 answers from the cache, counts unchanged
  (settled 47, unsettled 3).
- The task's per-study record for pass 3: GSE100021 is the one study whose inputs changed from pass
  2, and the one that sent requests, 2 of them, for $0.000129. Each of the other 49 has the same
  inputs as in pass 2 and sent no request.
- Jev's answer for GSE100021 did not change: p(yes) 0.96 as written and 0.14 reversed, both times.

`write-bounds` was not run after pass 3, so no committed file holds pass 3's answer. The store copy
is deleted.

## What this does and does not show

- The 50 were picked to be small. No group came near the 50-Series limit, so no study was refused
  and the refusal path is untested here.
- Matching the standard set does not measure accuracy. Jev's questions were tuned with the 50 in
  view, and models drew the standard set's bounds.
- The 11 studies answered before the kill were already in the gateway's cache when pass 1 ran, so
  pass 1 paid nothing. The test shows a killed pass loses no study and its rerun pays nothing for
  them. It does not show what happens to an answer paid for in the same second as the kill.
  A request in flight at the kill would not be in the task's record, and none was measured.
- `pass status` shows that pass 3 paid for exactly two requests, which is one study's two
  questions, but it does not name the study. The study's name comes from the task's per-study
  record.
- A rerun asks again about every study in its keys, and a cached answer costs nothing. It does not
  skip a study because the task already holds its answer, so a rerun's `cache_hits` count every
  answered study again.
