# Prototype: the model calls that draw a study's bounds

**Throwaway.** This folder answers one question for the bounding map (stage 4 of the
study-understanding roadmap): *what sequence of model calls draws a study's bounds (its GEO
Series and its papers) from its linked group?* It ran on the 50 studies of the standard set,
against the toy store, on 2026-11-21 between 04:36 and 04:50 UTC (a Saturday, so DeepSeek's
off-peak price applied). Nothing here is imported by `src/seqforge`.

Evidence classes: **measured** means counted from this run's files; **inferred** means reasoned
from those counts. Matching the standard set is not accuracy: its bounds were drawn by a small
model and checked by a large one, and no person has reviewed them.

## Answer in brief

- **Measured:** Jev's plain yes/no per linked record, with code placing the picked Series and
  any SuperSeries, drew the same bounds as the standard set on **all 50** studies. The writing
  model (DeepSeek Flash, thinking off) drew different bounds on **5 of the 13** groups that are
  not one Series and one paper. Jev sided with the standard set on every one of those 5 records.
- **Measured:** with thinking on, the writer matched the standard set on all 13, at about 3 times
  the cost and 4 times the time.
- **Measured:** a group of one Series and one paper (34 groups) or one Series and no paper (3)
  never needed the writer. The writer, run on all 37 anyway, placed every record as code's default
  does. Jev's probability for the one paper was 0.75 to 0.99.
- **Measured:** 118 of the writer's 121 quotes were found again by code in the field the writer
  named. Only 27 of the 118 back their claim, as Jev judged them, and none of the 42 quotes from an
  abstract does. Most claims stay inferred.
- **Measured:** the three linked Series that are older studies re-cited by this study's paper
  each carry a sign code can read: a paper of their own that the picked Series does not link.
  Jev answered no for all three (p 0.11 to 0.31), and the writer left all three out.
- **Measured:** all of it cost **$0.015** for the 50 studies in the sequence as run (median
  $0.0001 a study). This session's model calls, the variants included, cost **$0.075** in total.

## The call sequence as run

One study at a time; up to 8 calls in flight across studies.

| Step | Who | Input | Output |
|---|---|---|---|
| 0 | code | the linked group from the toy store (`read_group`, as the standard set's drafting pass reads it) | the shape (number of Series and papers, the SuperSeries), the picked Series' core, and for each other Series whether it links a paper the core does not (the re-cite sign) |
| 1 | DeepSeek Flash (`deepseek-flash`, thinking off, JSON mode) | `drafter_text`: each Series' title, type, dates, summary, design, citation, relation, linked papers and sample lines; each paper's title, journal, date, linked Series and abstract. The system prompt is the standard set's `RULES` plus a quote instruction. **Skipped for a group of one Series and at most one paper.** | `{"series": [entry], "papers": [entry], "left_out": [entry], "reason": str}`, where `entry` is `{"id": str, "quote": {"source": "GSE…/pmid:…", "field": str, "text": str} or null, "how": str}` |
| 2 | Jev (`jev-1.14.0`), in parallel with step 1 | the same text as step 1, as Jev's description | one `noul` (yes/no probability) per linked Series other than the picked one, and one per paper (questions below) |
| 3 | code | each writer quote, and the group's text | where the quote is found: in its named field, another field, another record, or nowhere, with character offsets. Matching ignores case, runs of white space, and curly against straight quote marks |
| 4 | Jev, after step 3 | each quote that code found, with the claim it backs | one `noul` per quote: does this passage give evidence for the claim? |
| 5 | code | steps 1 to 4 | the bounds file (format 1), then `check_bounds` |

**Jev's questions (step 2).** `target` is `GEO Series <picked>`, or `the Series under SuperSeries
<picked>`:

- Series: *Was GEO Series X deposited as part of the same piece of research as {target}: data
  made for it, rather than an older dataset that this research only re-uses or cites, a later
  dataset that re-uses its samples, or an unrelated study?*
- Paper: *Is paper pmid:N the paper that first reports the data of {target} (a preprint and its
  journal version both count), rather than a later paper that re-uses that data, a paper that
  reports another dataset of this group, or a paper linked by mistake?*

**How a claim gets its probability.** A Series in the study gets p = Jev's p(yes). A left-out
record gets p = 1 − p(yes). The picked Series (or each sub-Series, for a SuperSeries) and the
SuperSeries itself are code's own calls, so they get `p: null`. The format gives papers no claim
and so no `p`; their p(yes) is kept beside the file, in `results.json` (`paper_p`).

**How a claim gets its backing.** `quoted` only when code finds the words again *and* Jev gives
the passage-backs-claim question p ≥ 0.5. A quote that is found but does not back the claim is
kept as a passage and named in `see`, and the claim is `inferred`, with the writer's `how` as
its text.

The output for each study is `out/<ID>.bounds.yaml`, which passes `check_bounds` (49 files). An
answer the format check rejects is not written as a bounds file: the study ends refused, in
`out/<ID>.refused.yaml` with the problems (1 study, GSE131269). In the committed copies, a quote
from a paper's title or abstract is replaced by its character offsets, such as `[abstract chars
120-245]`. The full files are in the untracked `_logs/`.

## The four questions

### What the writer writes, what Jev answers, and where p comes from

The writer writes the lists, one quote per entry, and the reason line. Jev answers one neutral
yes/no per record, which does not depend on the writer, so the two calls run in parallel.
p comes from Jev, as described above.

**Measured, and what it suggests for the job.** On the 13 groups where the writer ran, it and Jev
disagreed on 5 records. On all 5, Jev agreed with the standard set (table below). With thinking
on, the writer agreed with the standard set on all 13. **Inferred:** a cheap job can let Jev and
code place the records, and keep the writer for the reason line and the quotes. A record where
the writer and Jev disagree is a natural "unsettled" mark. A writer that only explains a
placement it was given was **not tried**.

### Does a group of one Series and one paper need the writer?

**No, on this set.** 34 groups are exactly one Series and one paper, and 3 more are one Series
and no paper. (The standard set says 33 drew as one Series and one paper; the count here is of
groups, not of drawn bounds.) For all 37:

- the writer, run anyway, placed every record as code's default does: the Series in, the paper in;
- the standard set's bounds agree with that default on all 37;
- Jev's p(yes) for the single paper: 1 at 0.7–0.8, 5 at 0.8–0.9, 28 at 0.9–1.0. The lowest were
  GSE137213 (0.75) and GSE114951 (0.80).

Skipping the writer there saved $0.016 of the $0.025 the writer and its quote check cost on all
50. A wrongly linked paper never occurred in a one-paper group here. All 10 left-out papers in
the standard set sit in groups of two or more papers. So Jev's paper question is the only guard
on a one-paper group, and its lowest value was 0.75.

### Can a claim be backed by a passage code finds again?

**The words come back; the backing mostly does not.**

The writer ran on all 50 groups for this count and gave 121 quotes. Code found 118 again, each
in the field the writer named. One was not found and two were null.

| Quotes found again, by where they came from | Found | Back their claim (Jev p ≥ 0.5) |
|---|---|---|
| A paper's abstract | 42 | **0** |
| A Series title or a paper title | 32 | 5 |
| A Series' overall design | 31 | 14 |
| A Series' relation field (SuperSeries and sub-Series) | 6 | 6 |
| A Series' summary | 6 | 2 |
| A Series' sample lines | 1 | 0 |
| **All** | **118** | **27** |
| Of these, quotes for a left-out record | 15 | 1 |

- The one left-out claim with real backing is GSE234962's later Series GSE314726. Its design text
  says two of its samples were "re-used from GSE234962" (p 0.92).
- In the sequence's 49 bounds files, 8 claims are `quoted` and 61 `inferred`.
- Jev's backing judgement spread widely (118 quotes): 29 at 0.0–0.1, 16 at 0.1–0.2, 11 at
  0.2–0.3, 13 at 0.3–0.4, 22 at 0.4–0.5, 10 at 0.5–0.6, 7 at 0.6–0.7, 7 at 0.7–0.8, 2 at
  0.8–0.9, 1 at 0.9–1.0. The line at 0.5 cuts through the thickest part. The writer was asked to
  prefer a linking sentence over a title, and still mostly quoted titles and abstract sentences
  that describe a record rather than tie it to the study.
- **Inferred:** the records state a link (a relation field, "re-used from", "reanalysis of")
  only now and then. A quote that grep-checks is easy to get and is not evidence by itself.
  Checking that the passage backs the claim is the step that matters, and code cannot do it.

### How the sequence keeps out an older Series that a paper only re-cites

Three linked Series in the set are older studies whose data this study's paper re-uses:
GSE143476 (in GSE306935's group), GSE115571 (GSE254687) and GSE107190 (GSE147401).

- **Code's sign.** Each links a paper of its own that the picked Series does not link. All
  three are out in the standard set. Of the 5 other linked Series with no such paper, 4 are in.
  The fifth, GSE314726, is a *later* Series that re-uses the picked Series' samples and says so
  in its design text.
- **Jev.** p(yes) 0.26, 0.11 and 0.31 for the three: out. Their own papers got p(yes) 0.04 each.
  Two of the three are among the four records whose p(yes) fell between 0.2 and 0.8.
- **Writer.** It left all three out, with or without thinking. Its one Series error went the
  other way: it left out GSE241275, a sibling Series in GSE241718's group with no paper of its
  own (code sign: no own paper; Jev 0.92 yes; standard set: in).
- **Inferred:** the cheap guard is to let code's sign and Jev's answer check each other. They
  agree on 7 of the 8 non-picked Series, and the eighth (GSE314726) is the one a later re-use
  explains.

## Cost and time

Per study, for the sequence as run: Jev on every study, and the writer and the quote check only
on the 13 groups that are not one Series and one paper. Costs are the gateway's metered first
cost. Times are seconds per call, from calls the gateway did not answer from its cache. Three
trial studies were answered from the cache in the final run and are left out of the times.

| | Median | Max | Total, 50 studies |
|---|---|---|---|
| Cost per study | $0.00011 | $0.0012 | **$0.0146** (Jev $0.0053, writer $0.0089, quote check $0.0004) |
| Time per study, one Series and at most one paper (Jev only), n = 36 | 0.29 s | 0.61 s | |
| Time per study with the writer: Jev beside writer then quote check, n = 11 | 3.24 s | 3.64 s | |
| Time per study, all, steps run one after another, n = 47 | 0.32 s | 4.00 s | 49 s summed; **22 s wall** for the whole run with 8 in flight (writer on all 50 included) |

Per call: the writer took 2.0 s median and 3.3 s max (47 calls, median 2,400 tokens in and 230
out, $0.00043). Jev took 0.32 s median and 0.63 s max (44 calls, median 2,300 tokens in, max
6,100).

**Thinking.** The main run turned thinking **off** (`thinking=False`, as seqforge's drafting kind
does). As a variant, the writer ran with thinking **on** over the 13 groups. Median and max were
13 s and 89 s a call, with a median of 2,080 reasoning tokens and $0.0020. The 13 calls cost
**$0.029**, against $0.0089 with thinking off, plus $0.008 for one failed first attempt on
GSE254687 (two attempts, 89 s). It matched the standard set on all 13, against 8 of 13 off. The reasoning tokens are counted inside
`completion_tokens`, and the gateway prices them as output. DeepSeek's own invoice was not
checked.

**Model name.** Every writer call sent `deepseek-flash`, which the gateway accepted and priced.
The gateway's reply to the client carries no model name. Its keys are `attempts`, `cached`,
`cost_usd`, `first_cost_usd`, `text` and `usage`, so the name the provider's response reported
is visible only in the gateway's own record, which this prototype did not read.

**Spend this session** (gateway meters): smoke test and trials $0.005, the 50-study run $0.028
(including $0.016 for the writer and its quote check on the 37 single-Series groups), the
thinking variant $0.037, the negated-question check $0.005. **Total about $0.075**, under the $5
limit.

## Where the output differs from the standard set's bounds

The sequence as run (writer placing, Jev giving p) differs on 5 studies:

| Study | Record | Standard set | Sequence | Jev p(yes) | Writer with thinking on |
|---|---|---|---|---|---|
| GSE241718 | GSE241275 | in | out | 0.92 | in |
| GSE132886 | pmid:39010112 | out | in | 0.15 | out |
| GSE101112 | pmid:32884834 | out | in | 0.13 | out |
| GSE234962 | pmid:41390837 | out | in | 0.04 | out |
| GSE131269 | pmid:31279658 | out | **refused**: placed both in `papers` and in `left_out`, so the format check rejects the file | 0.10 | out |

Placement from Jev alone (p(yes) > 0.5 is in) differs on none. Every record of the 13 groups
that are not one Series and one paper is in [`per_record.md`](per_record.md).

## How Jev's probabilities spread

The line between settled and unsettled will be drawn over these values. Counts per bucket of
width 0.1; "0.8" means 0.8 ≤ p < 0.9.

**Jev's p(yes), one per record asked (70 records: every linked Series but the picked one, and
every paper):**

| 0.0 | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 |
|---|---|---|---|---|---|---|---|---|---|
| 7 | 5 | 1 | 1 | 0 | 0 | 0 | 1 | 10 | 45 |

Nothing fell between 0.4 and 0.7. The four between 0.2 and 0.8 are GSE143476 (0.26) and
GSE107190 (0.31), two of the three re-cited older Series, and two single papers, GSE137213's
(0.75) and GSE114951's (0.80, on the edge).

**The claims of the sequence's 49 bounds files** (Series kept and records left out; 49 more are
code's calls with `p: null`): 1 at 0.0, 1 at 0.6, 1 at 0.7, 2 at 0.8, 15 at 0.9. The one at 0.0
is the writer's wrong exclusion of GSE241275. **Papers kept** (outside the format): 1 at 0.0, 2
at 0.1, 1 at 0.7, 9 at 0.8, 35 at 0.9. The three below 0.2 are the writer's wrong keeps. Its
fourth, in GSE131269, ended in the refused study.

**The same questions asked the other way round** (`jev_negated.py`): "is X an older dataset …
rather than …". For 67 of 70 records, the negated answer gives the same placement. The gap
between p(yes) and 1 − p(negated yes) had a median of 0.07, a 90th percentile of 0.16 and a
maximum of 0.78. The three that flip are the older studies' own papers in GSE306935 and
GSE147401, and GSE131269's second paper. The negated wording lists three ways to be out, and
seems harder for Jev to apply. **Inferred:** asking both ways and requiring agreement would mark
exactly the re-cite cases and one two-paper case as unsettled, while every plain answer stays
right.

## What surprised me

1. **Jev alone matched every bound, and the writer caused every difference.** The chain on the
   map has the writer write the lists and Jev check them. On this set the lists were better
   when Jev drew them and the writer only explained them.
2. **No quote from an abstract backed its claim.** Abstracts describe the research and never
   say which GEO Series or which paper is whose. Backing came from the record's own relation
   and design fields.
3. **Papers carry no claim in the bounds format,** so they carry no `p` and no backing. Yet 4 of
   the writer's 5 errors were papers. A paper's probability currently has nowhere to go.
4. **The writer with thinking off changed its paper picks under a one-sentence prompt edit.**
   GSE234962 kept pmid:41509263 in the first trial and pmid:41390837 in the second. Thinking on
   removed every difference on this set, for 13 s median per call.
5. **37 single-Series groups, not 33.** The number of groups that need no writer is a count of
   groups, which is not the number of drawn bounds of that shape.

## Limits

- The Jev questions were written after reading the record-links research note, and the
  quote-check wording was adjusted once on 3 studies. No study was held out.
- Jev saw only the records and abstracts; so did the standard set's drafting model. The standard
  set's checker also read the full paper for 7 studies; Jev matched those without it.
- One run per call. The writer was not re-run to measure how much it varies with no prompt change.

## Files

| File | Holds | Committed |
|---|---|---|
| `run.py` | the sequence; `--all-writer` runs the writer on single-Series groups too, `--thinking` the thinking variant | yes |
| `jev_negated.py` | the negated questions and the agreement count | yes |
| `summarize.py`, `tables.py` | the aggregates and tables above | yes |
| `per_record.md` | every record of the 13 groups that are not one Series and one paper | yes |
| `smoke.py` | one tiny call to each model | yes |
| `out/<ID>.bounds.yaml` | the sequence's bounds for each study, paper quotes as offsets; `out/GSE131269.refused.yaml` for the one refused study | yes |
| `results.json` | per study: shape, placements, the standard set's, Jev p(yes), quote checks (ids, fields, offsets, backing p), cost, time | yes |
| `jev_negated.json` | per record: p(yes) both ways | yes |
| `_logs/<ID>/` | raw replies, full bounds files with paper quotes (paper-store text) | **no**, git-ignored |

Rerun from the worktree root, with the tunnel up and `LABLLM_GATEWAY`, `LABDATA_STORE` and
`LABDATA_PAPERS` set, using seqforge's pixi environment's Python:

```bash
python prototypes/bounding_calls/run.py --all-writer
python prototypes/bounding_calls/run.py --only <the 13 ids> --thinking
python prototypes/bounding_calls/jev_negated.py
python prototypes/bounding_calls/summarize.py
```
