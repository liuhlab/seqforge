# The study chain on the 50 studies, after the join

Dated 2026-11-27, for [#712](https://github.com/liuhlab/seqforge/issues/712), on the toy store of
the standard set, build `20261107T163933Z-subset`. **Both jobs paid $0.0000: every answer came from
the gateway's cache. The bounding job wrote 50 files from kind `2026.11.3`: 47 settled, 3
unsettled, none refused. The reading job wrote 50 files from kind `2026.11.10`: 38 settled, 12
unsettled, none refused. No study's ending moved since 2026-11-26, and no claim changed. Each
bounds file gained a map of its records' titles, and each reading row gained the `biosample_accession` its
record links, on 785 of 787 samples. A full `study run` of the 50 in a new folder wrote the same
100 files byte for byte, and `report --study` wrote a report for each of the 50. All 100 job files
pass their check.**

The question: after the merge that joined the study chain, the compiler and the report, what do
the two jobs write for the 50 studies, what does that cost, and which studies' endings or claims
moved? And does the chain run end to end, report included, the way a user runs it?

Evidence class: everything here is **measured** in one pass of each job, then one `study run`, on
one laptop on the evening of 2026-11-27 (Boston time; 2026-11-28 in UTC), unless a line says
**inferred**. The comparison is with the files of
[both jobs on the 50 again](both-jobs-on-the-50-again-2026-11-26.md). No number here scores a file
against the standard set or against the sources. Scoring is a separate effort.

## Method

Every step ran seqforge `9209509`, liulab-llms 2026.11.10 through the lab gateway, and liulab-data
`6377bd2`. Each role took the kind's default model: `typesafe/jev-1.14.0` as Jev in both jobs, and
`deepseek/deepseek-flash` with thinking on as the reading job's writer. `<scratch>` is a scratch
folder, `<toy>` is the folder that holds the toy store's two parts, and `<run>` is the new folder
of the full run, under the lab data folder and outside git:

```bash
export LABLLM_GATEWAY=http://127.0.0.1:8000
export LABDATA_STORE=<toy>/geo-sra-store LABDATA_PAPERS=<toy>/paper-store
export LABDATA_ONTOLOGIES=<run>/ontologies

# Step 1: the repo's job files.
seqforge pass start study-bounds --ceiling-usd 2 --units-at-once 4 \
  --label study-bounds-50-712 --task <scratch>/task-bounds
seqforge pass start study-reading --ceiling-usd 3 --units-at-once 4 \
  --label study-reading-50-712 --task <scratch>/task-reading
rm evals/standard-set/*/job-bounds.yaml evals/standard-set/*/job-reading.yaml
seqforge study write-bounds --out evals/standard-set --name job-bounds.yaml --task <scratch>/task-bounds
seqforge study write-reading --out evals/standard-set --name job-reading.yaml --task <scratch>/task-reading
seqforge study check-bounds evals/standard-set/<ID>/job-bounds.yaml     # once per study
seqforge study check-reading evals/standard-set/<ID>/job-reading.yaml   # once per study

# Step 2: a full run of the 50, the way a user runs it. <run>/studies holds only pick.yaml.
cd <run>
seqforge study run --studies studies --ceiling-usd 5
seqforge report --study studies/<ID>                                    # once per study
```

**The ontology store.** The run took a copy of the store made for the run of 2026-11-26, so every
vocabulary sits at the same snapshot as then, `ncbitaxon-2026-11-26` included. The reading files'
`ontology_snapshot.date` is 2026-11-28, the UTC day the pass opened the store.

**What the hash list shows.** A SHA-256 list of every file under `evals/standard-set/`, taken
before and after, differs in the 50 `job-bounds.yaml` and the 50 `job-reading.yaml` files only. The
set's own `bounds.yaml` files and the five worked readings are untouched.

**Spend.** The issue allowed $2 for bounding and $3 for reading. Step 1 paid $0.0000, so step 2
took a ceiling of the whole $5 and paid $0.0000 too.

## The bounding job

| Measure | Value |
| --- | --- |
| Paid | $0.0000 |
| Requests sent | 0 |
| Answers from the gateway's cache | 92 |
| Dollars when each answer was first paid | $0.0104 |
| Wall time | 11 seconds |

| Ending | Studies |
| --- | --- |
| Settled | 47 |
| Unsettled | 3 |
| Refused | 0 |

**Nothing moved.** The three unsettled studies are the same three, and each disagrees on the same
paper: GSE131269 on pmid:32494643, GSE147401 on pmid:29167199 and GSE306935 on pmid:32198156.

**What changed in the files.** Each of the 50 files gained one field, `titles`: the title of each
record it places, kept or left out, by the record's id. With that field taken out, every file holds the same content
as the file of 2026-11-26. `check-bounds` accepts all 50.

## The reading job

| Measure | Median | 90th percentile | Total |
| --- | --- | --- | --- |
| Dollars paid in this pass | $0.0000 | $0.0000 | $0.0000 |
| Dollars when each answer was first paid | $0.0301 | $0.0444 | $1.5370 |
| Requests sent | 0 | 0 | 0, and 1,774 answers from the cache |
| Seconds | 2.4 | 3.6 | 125, summed over studies run 4 at once |

The pass took 38 seconds of wall time. A first read of the 50 costs $1.537, the same as on
2026-11-26: the new kind sent the models the same text as the old one.

| Ending | Studies | File |
| --- | --- | --- |
| Settled | 38 | Written, and accepted by `check-reading` |
| Unsettled | 12 | Written, and accepted by `check-reading` |
| Refused | 0 | None |

**Nothing moved.** The 12 unsettled studies are the 12 of 2026-11-26. Each disagrees where it did
then:

| Study | Where Jev's two askings disagree |
| --- | --- |
| GSE106105 | Factor `treatment`: its baseline |
| GSE126954 | GSM4318946: its level of `age` |
| GSE140511 | Factor `brain_region`: varied on purpose, and its baseline |
| GSE146013 | Factor `disease_stage`: its baseline |
| GSE234962 | Factor `strain`: varied on purpose |
| GSE254687 | Factor `duration`: varied on purpose |
| GSE255902 | GSM8082452: its level of `dose` |
| GSE288106 | GSM8759027: its level of `disease` |
| GSE302826 | Factor `treatment`: its baseline; GSM9111828: its level of `treatment` |
| GSE310667 | Factors `age` and `cell_status`: varied on purpose; `cell_status`: its baseline |
| GSE329430 | Factor `treatment`: varied on purpose; factor `cell_type`: its baseline |
| GSE97461 | GSM2565660: its level of `treatment` |

**What changed in the files.** Two fields, and nothing else:

- **`biosample_accession`** on each sample row whose GEO Sample record links exactly one BioSample.
  785 of the 787 samples carry one. The two without are GSM8085924 and GSM8085925 of GSE254687:
  the store holds only a platform link for each, and no BioSample link. This is the key that
  `seqforge run --study` joins a reading into `sample_metadata.tsv` by.
- **`ontology_snapshot.date`**, from 2026-11-26 to 2026-11-28, the UTC day of this pass.

With those two taken out, every file holds the same content as the file of 2026-11-26.
`check-reading` accepts all 50.

## The full run and its reports

`<run>/studies` started with the standard set's `pick.yaml` and nothing else: no study file, no
bounds, no reading. `study run` ran the bounding pass, wrote each study's `bounds.yaml`, ran the
reading pass over those bounds and wrote each `reading.yaml`.

| Step | Exit | Result |
| --- | --- | --- |
| `pass start study-bounds` | 0 | 47 settled, 3 unsettled; 92 answers from the cache, $0.0000 |
| `study write-bounds` | 0 | 50 files written |
| `pass start study-reading` | 0 | 38 settled, 12 unsettled; 1,774 answers from the cache, $0.0000 |
| `study write-reading` | 0 | 50 files written |

The whole run took 30 seconds at the default 32 units at once. Its 50 `bounds.yaml` files are the
same bytes as the repo's `job-bounds.yaml`, and its 50 `reading.yaml` files the same bytes as the
repo's `job-reading.yaml`. Here the reading pass read bounds the job drew, where step 1 read the
standard set's own bounds. Every request still came from the cache, so the reader sent the same
text over both (**inferred** from the zero requests).

`report --study` exited 0 for all 50 folders, each writing `report.html` and `report.json`, about
120 KB and 70 KB a study. No report names a path on the machine. A report shows the folder's bounds
and reading, but not how the job ended the study: an unsettled study's page looks like a settled
one's. The ending lives only in the pass's task.

Three of the reports are on the docs site as examples: GSE112068, GSE272899 and GSE329430. They
quote only GEO records and a PubMed abstract.

## What this does and does not show

- Every number here is measured from the two tasks, the 100 files, the run folder and its 50
  reports, except the line marked **inferred**. Nothing here says whether a reading is right.
- Every answer came from the cache. The run shows that the join changed what the files hold and
  not what the models were asked. It does not show what a read from scratch costs today beyond the
  first-paid totals, or how long it takes; the last read from scratch took about 22 minutes at four
  studies at once ([the reading job on the 50](reading-job-on-the-50-2026-11-24.md)).
