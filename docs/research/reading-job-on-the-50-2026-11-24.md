# The reading job on the 50 studies

Dated 2026-11-24 (local time; the run was on 2026-11-25 in UTC), for
[#637](https://github.com/liuhlab/seqforge/issues/637), on the toy store of the standard set, build
`20261107T163933Z-subset`. **The reader read all 50 studies: 39 settled, 11 unsettled, none
refused. `write-reading` wrote a `job-reading.yaml` for all 50, and `check-reading` accepted all
50. The pass paid $1.52 over 24 minutes at 4 studies at once: a median of $0.031 and 106 seconds a
study. No writer answer needed its retry, and 11 of 927 quotes were not found in their source.**

The question: does the reading job, `seqforge pass start study-reading` and then
`seqforge study write-reading`, read each of the 50 studies into a reading that passes its check,
or list the study with the reason it stopped, and what does that cost?

Evidence class: everything here is **measured** in one pass on one laptop. No number here scores
the readings against the standard set's five worked readings or against the sources. Scoring is a
separate effort.

## Method

The pass ran seqforge `3f2fa48` with liulab-llms 2026.11.8 and liulab-data `6377bd2`, kind version
`2026.11.3`, and each role's default model: `deepseek/deepseek-flash` with thinking on as the
writer, and `typesafe/jev-1.14.0` as Jev. It read the toy store of the 50 studies through the lab
gateway, the ontology store described below, and a new empty review clone. `<scratch>` is a scratch
folder that holds the task folder and the review clone, and `<toy>` is the folder that holds the
toy store's two parts:

```bash
export LABLLM_GATEWAY=http://127.0.0.1:8000
export LABDATA_STORE=<toy>/geo-sra-store LABDATA_PAPERS=<toy>/paper-store
export LABDATA_ONTOLOGIES=<ontology store>
export SEQFORGE_REVIEWS=<scratch>/clone
seqforge pass start study-reading --ceiling-usd 3 --units-at-once 4 \
  --label study-reading-50-637 --task <scratch>/task50
seqforge pass status study-reading --task <scratch>/task50
seqforge study write-reading --out evals/standard-set --name job-reading.yaml --task <scratch>/task50
seqforge study check-reading evals/standard-set/<ID>/job-reading.yaml   # once per study
```

The first start of the pass found the gateway down and stopped at once, with all 50 units
unfinished, no request sent and $0 paid. The same command, run again once the gateway was back,
is pass 2, and every number below is from pass 2.

The ontology store held 13 vocabularies, each at one snapshot: `cellosaurus-57.0`, `chebi-257`,
`cl-2026-11-13`, `efo-3.96.0`, `fbdv-2026-11-02`, `hsapdv-2025-01-23`, `mmusdv-2025-01-23`,
`mondo-2026-11-03`, `ncbitaxon-2026-11-25`, `pato-2026-10-01`, `uberon-2026-11-05`,
`wbbt-2025-08-19` and `wbls-2026-04-16`.

Three of the 50, GSE100021, GSE235532 and GSE241718, had been read earlier the same day in a
pilot with the same code against the store before EFO was added. In pass 2 each sent one request,
which held its ontology-term questions, and took every other answer from the gateway's cache.

## How the 50 ended

| Ending | Studies | File |
| --- | --- | --- |
| Settled | 39 | Written, and accepted by `check-reading` |
| Unsettled | 11 | Written, and accepted by `check-reading` |
| Refused | 0 | None |

The 50 files hold 787 samples. Each unsettled study has at least one question that Jev answered
one way as written and the other way when it was asked a second time:

| Study | Where Jev's two answers disagree |
| --- | --- |
| GSE106105 | Factor `treatment`: its baseline |
| GSE126954 | GSM4318946: its level of `age` |
| GSE140511 | Factor `brain_region`: whether it was varied on purpose, and its baseline |
| GSE146013 | Factor `disease_stage`: its baseline |
| GSE234962 | Factor `strain`: whether it was varied on purpose |
| GSE254687 | Factor `duration`: whether it was varied on purpose |
| GSE255902 | GSM8082452: its level of `dose` |
| GSE302826 | Factor `treatment`: its baseline; GSM9111828: its level of `treatment` |
| GSE310667 | Factors `age` and `cell_status`: whether each was varied on purpose; `cell_status`: its baseline |
| GSE329430 | Factor `treatment`: whether it was varied on purpose; factor `cell_type`: its baseline |
| GSE97461 | GSM2565655: its level of `treatment` |

In two of them the disagreement is on a factor that the file does not hold. GSE254687's `duration`
and GSE329430's `treatment` were listed by the writer and dropped because no sample is at any of
their levels, so the ending names a factor that the file does not have.

## Cost and time per study

From `pass status` for pass 2. The maximum comes from the task's per-study record, since
`pass status` gives the 90th percentile and not the maximum. "First paid" is what each answer cost
when it was first paid, so for the three pilot studies it is the pilot's cost.

| Measure | Median | 90th percentile | Max | Total |
| --- | --- | --- | --- | --- |
| Dollars paid in pass 2 | $0.0307 | $0.0454 | $0.0555 (GSE310667) | $1.5197 |
| Dollars when each answer was first paid | $0.0307 | $0.0454 | $0.0555 (GSE310667) | $1.5667 |
| Seconds | 106 | 160 | 232 (GSE182981) | 5,483, summed over studies run 4 at once |
| Requests | 34 | 52 | 68 (GSE101112) | 1,685, and 89 answers from the cache |

The pass took 24 minutes 3 seconds of wall time, from 02:03:07 to 02:27:10 UTC. Without the three
pilot studies, the 47 read from scratch have a median of $0.0309 and 111 seconds. The pilot's
$0.016 a study came from three of the cheaper studies: the median of the 50 is about twice that.

| Study | Ending | Samples | First paid | Paid in pass 2 | Seconds | Requests | Writer answers | Quotes | Quotes not found | Searches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GSE100021 | Settled | 8 | $0.0117 | $0.0001 | 1 | 1 | 1 | 11 | 0 | 4 |
| GSE101112 | Settled | 32 | $0.0470 | $0.0470 | 135 | 68 | 1 | 24 | 0 | 8 |
| GSE106105 | Unsettled | 11 | $0.0348 | $0.0348 | 128 | 26 | 1 | 20 | 0 | 5 |
| GSE112068 | Settled | 23 | $0.0319 | $0.0319 | 160 | 50 | 1 | 27 | 0 | 7 |
| GSE114951 | Settled | 12 | $0.0214 | $0.0214 | 59 | 28 | 1 | 10 | 0 | 3 |
| GSE117724 | Settled | 20 | $0.0357 | $0.0357 | 144 | 44 | 1 | 20 | 0 | 6 |
| GSE125850 | Settled | 12 | $0.0457 | $0.0457 | 136 | 28 | 1 | 26 | 0 | 6 |
| GSE126954 | Unsettled | 8 | $0.0305 | $0.0305 | 96 | 20 | 1 | 20 | 0 | 7 |
| GSE129928 | Settled | 12 | $0.0321 | $0.0321 | 144 | 28 | 1 | 21 | 0 | 3 |
| GSE131269 | Settled | 24 | $0.0387 | $0.0387 | 180 | 52 | 1 | 16 | 0 | 6 |
| GSE132886 | Settled | 21 | $0.0323 | $0.0323 | 171 | 46 | 1 | 18 | 0 | 5 |
| GSE135532 | Settled | 16 | $0.0265 | $0.0265 | 77 | 36 | 1 | 14 | 1 | 8 |
| GSE137213 | Settled | 19 | $0.0277 | $0.0277 | 81 | 42 | 1 | 12 | 0 | 6 |
| GSE140511 | Unsettled | 20 | $0.0374 | $0.0374 | 90 | 44 | 1 | 20 | 0 | 3 |
| GSE142633 | Settled | 13 | $0.0235 | $0.0235 | 130 | 30 | 1 | 13 | 0 | 3 |
| GSE146013 | Unsettled | 24 | $0.0340 | $0.0340 | 155 | 52 | 1 | 20 | 0 | 6 |
| GSE147401 | Settled | 30 | $0.0399 | $0.0399 | 87 | 64 | 1 | 29 | 0 | 6 |
| GSE151164 | Settled | 12 | $0.0244 | $0.0244 | 69 | 28 | 1 | 14 | 1 | 5 |
| GSE153881 | Settled | 15 | $0.0308 | $0.0308 | 86 | 34 | 1 | 35 | 0 | 5 |
| GSE165978 | Settled | 9 | $0.0419 | $0.0419 | 132 | 22 | 1 | 34 | 0 | 8 |
| GSE175410 | Settled | 15 | $0.0246 | $0.0246 | 96 | 34 | 1 | 10 | 0 | 4 |
| GSE182981 | Settled | 18 | $0.0485 | $0.0485 | 232 | 40 | 1 | 23 | 0 | 6 |
| GSE196554 | Settled | 12 | $0.0198 | $0.0198 | 107 | 28 | 1 | 14 | 0 | 7 |
| GSE208154 | Settled | 11 | $0.0202 | $0.0202 | 64 | 26 | 1 | 16 | 3 | 3 |
| GSE222631 | Settled | 24 | $0.0239 | $0.0239 | 111 | 52 | 1 | 12 | 0 | 4 |
| GSE234962 | Unsettled | 4 | $0.0230 | $0.0230 | 95 | 12 | 1 | 14 | 0 | 4 |
| GSE235532 | Settled | 24 | $0.0206 | $0.0001 | 3 | 1 | 1 | 24 | 0 | 2 |
| GSE240375 | Settled | 10 | $0.0216 | $0.0216 | 123 | 24 | 1 | 7 | 0 | 4 |
| GSE241718 | Settled | 8 | $0.0150 | $0.0001 | 1 | 1 | 1 | 17 | 0 | 4 |
| GSE243994 | Settled | 12 | $0.0254 | $0.0254 | 71 | 28 | 1 | 12 | 0 | 4 |
| GSE253651 | Settled | 18 | $0.0278 | $0.0278 | 153 | 40 | 1 | 19 | 4 | 5 |
| GSE254687 | Unsettled | 21 | $0.0454 | $0.0454 | 186 | 46 | 1 | 20 | 0 | 9 |
| GSE255902 | Unsettled | 22 | $0.0485 | $0.0485 | 136 | 48 | 1 | 19 | 0 | 8 |
| GSE256266 | Settled | 4 | $0.0285 | $0.0285 | 83 | 12 | 1 | 15 | 0 | 4 |
| GSE272899 | Settled | 9 | $0.0276 | $0.0276 | 105 | 22 | 1 | 16 | 0 | 5 |
| GSE274290 | Settled | 7 | $0.0328 | $0.0328 | 105 | 18 | 1 | 17 | 0 | 9 |
| GSE275435 | Settled | 12 | $0.0296 | $0.0296 | 76 | 28 | 1 | 15 | 1 | 3 |
| GSE281157 | Settled | 12 | $0.0315 | $0.0315 | 79 | 28 | 1 | 15 | 0 | 4 |
| GSE288106 | Settled | 16 | $0.0254 | $0.0254 | 111 | 36 | 1 | 10 | 0 | 6 |
| GSE288182 | Settled | 9 | $0.0309 | $0.0309 | 154 | 22 | 1 | 12 | 0 | 5 |
| GSE302826 | Unsettled | 18 | $0.0407 | $0.0407 | 105 | 40 | 1 | 23 | 0 | 9 |
| GSE306935 | Settled | 12 | $0.0239 | $0.0239 | 115 | 28 | 1 | 13 | 0 | 5 |
| GSE310667 | Unsettled | 16 | $0.0555 | $0.0555 | 145 | 36 | 1 | 41 | 0 | 14 |
| GSE314221 | Settled | 16 | $0.0323 | $0.0323 | 90 | 36 | 1 | 17 | 0 | 6 |
| GSE329430 | Unsettled | 20 | $0.0380 | $0.0380 | 119 | 44 | 1 | 27 | 1 | 16 |
| GSE335500 | Settled | 12 | $0.0417 | $0.0417 | 125 | 28 | 1 | 20 | 0 | 8 |
| GSE60340 | Settled | 24 | $0.0294 | $0.0294 | 101 | 52 | 1 | 22 | 0 | 12 |
| GSE77720 | Settled | 23 | $0.0230 | $0.0230 | 72 | 50 | 1 | 13 | 0 | 5 |
| GSE78252 | Settled | 18 | $0.0304 | $0.0304 | 89 | 40 | 1 | 19 | 0 | 5 |
| GSE97461 | Unsettled | 19 | $0.0338 | $0.0338 | 167 | 42 | 1 | 21 | 0 | 9 |

## Writer retries and quotes

- **Writer retries: none.** Every study's first writer answer could be used.
- **Quotes: 927 checked, 11 not found**, in 6 studies: GSE253651 (4 of 19), GSE208154 (3 of 16),
  and one each in GSE135532, GSE151164, GSE275435 and GSE329430. A quote that is not found backs
  no claim, so its claim is `inferred`.
- **Assay guides:** the pull-down guide went to 5 studies, GSE117724, GSE125850, GSE129928,
  GSE147401 and GSE235532.

## Searches and terms

The pass made 299 searches of the ontology store. No study searched a vocabulary that the store
does not hold. The table counts the terms in the 50 files by vocabulary: the studies that have at
least one, the distinct ids over all 50, and the samples whose claim carries one. A claim under
`shared` counts once for each sample. `sex` takes one of the format's own words, such as
`female`, and not an ontology id, so it is counted apart.

| Vocabulary | Studies | Distinct ids | Samples |
| --- | --- | --- | --- |
| NCBI Taxonomy | 50 | 5 | 787 |
| EFO | 47 | 6 | 713 |
| PATO (`healthy` only) | 40 | 1 | 502 |
| UBERON | 25 | 24 | 401 |
| Cell Ontology (CL) | 14 | 16 | 174 |
| MONDO | 13 | 6 | 144 |
| WBls | 10 | 6 | 118 |
| WBbt | 9 | 5 | 103 |
| ChEBI | 10 | 10 | 89 |
| HsapDv | 4 | 2 | 63 |
| Cellosaurus | 4 | 4 | 53 |
| FBdv | 1 | 1 | 24 |
| MmusDv | 2 | 1 | 23 |
| The format's own `sex` words | 33 | 3 | 500 |

By label, out of the samples whose claim has a text: species 787 of 787 have a term, assay 713 of
787, disease 646 of 777, tissue 504 of 744, `sex` 500 of 521, cell type 174 of 327, development
stage 228 of 687, cell line 53 of 102 and treatment 89 of 466.

**No mouse `adult` got a term.** The development stage `adult` is written with no term in 17 mouse
studies, and it gets a term for every human, worm and fly study that writes it. Code maps `adult`
to the species' adult stage by searching for the whole name `adult stage` or `adult`. HsapDv,
WBls and FBdv each hold a term by that name. MmusDv names its adult stage `mature stage`
(`MmusDv:0000110`), so both searches find only longer names, such as `young adult stage`, and code
writes no term.

## What this does and does not show

- Every number here is measured from the pass's task record and the 50 files. Nothing here says
  whether a reading is right.
- The three pilot studies took most of their answers from the cache, so their paid cost and their
  time are not those of a first read. The first-paid column gives their cost.
- The pass ran 4 studies at once, which is up to 16 model requests in flight. The gateway did not
  push back, so a lower setting was not tried.
- The run stopped once on a gateway that did not answer, before any request. The rerun resumed it
  in the same task folder. That shows resuming after a stop that answered nothing, and not after a
  stop in the middle of a study.
