# The candidate studies for the standard set, on one build of liulab-data's store

Measured 2026-11-19 for [#583](https://github.com/liuhlab/seqforge/issues/583), on build
`20261107T163933Z` of liulab-data's store. **15,879 Series fit a direction (aging,
neurodegeneration or *C. elegans*), in 10,399 linked groups. The median Series has 12 samples and
30 GB of reads, 8,164 groups are one Series alone, and 99.5% of the linked papers have an abstract
in liulab-data.** Two things in the result are not what they look like: one paper glues 20,338
Series into a single group, and 3,703 Series read as having no paper on a build that holds no DOI
links.

The question: for sequencing Series that fit one of the three directions, how big is each Series,
what is it linked to, how many papers does it have, does liulab-data hold each paper's abstract,
and what is the assay mix? This is a measurement of the pool. It picks no study and sets no cap.

Evidence classes: **measured** means counted in this run's output. **Read** means seen in
liulab-data's code or docs, or in a file of the store. **Inferred** means reasoned and not checked.

Terms: a **Series** is a GEO Series (`GSE…`), a **Sample** a GEO Sample (`GSM…`). An
**experiment** is an SRA experiment (`SRX…`) and a **run** an SRA run (`SRR…`). A **target
Series** is a Series whose type names high-throughput sequencing. A **build** is one set of the
store's tables, named by the UTC time it started.

## Where the store is and how to open it

| Item | Value | How known |
| --- | --- | --- |
| Cluster | the lab's GPU cluster, ssh alias `arc` | read: liulab-data's `store-maintenance` skill |
| GEO/SRA store root | `<datalake>/geo-sra-store`, on the cluster's shared scratch | read: a listing of the folder |
| Paper store root | `<datalake>/paper-store`, beside it | read: the per-user pointer file `~/.cache/liulab-data/papers.toml` on the cluster |
| Build | `20261107T163933Z`, switched to on 2026-11-07, 10.9 GB | read: `labdata-store.toml` and `README.md` in the store root |
| Made by | liulab-data commit `e6962a5` | read: the store's `README.md` |
| Snapshots it read | `geo-series`, `sra-accessions`, `bioproject` and `biosample`, all dated 2026-11-07 | read: the store's `README.md` |
| Target Series | 200,132 listed, 200,131 held | read: the build's `README.md` |
| Series records | 207,113 | measured |
| Paper tables | written 2026-11-17; last daily pass 2026-11-18 | read: file dates and `paper-status.json` |
| On this Mac | no store, no pointer file, `LABDATA_STORE` unset | read |

`<datalake>` stands for the full path, which stays out of this repo. A later session finds it on
the cluster: the paper store's pointer file gives `<datalake>/paper-store`, and the GEO/SRA store
is the folder `geo-sra-store` beside it. **The GEO/SRA store has no pointer file there, and
`LABDATA_STORE` is not set in a non-interactive shell** (read), so `labdata.open_store()` needs
the root as its argument.

Google Cloud Storage holds the source of truth for both stores, and the cluster folder is the
work disk a refresh runs on (read: liulab-data `docs/how-to/store-gcs.md`). The store has no query
command. Read it with `labdata.open_store(root)` in liulab-data's pixi environment, or with plain
DuckDB over the files in `builds/<build>/`; the store's own `README.md` shows both.
**liulab-data's reader keeps DuckDB's spill files under `~/.cache/liulab-data/duckdb`** (read:
`src/labdata/store/reader.py`). On the cluster that is the home folder, which must not be written,
so this measurement uses plain DuckDB with its own spill folder.

## The tables the measurement reads

All in `builds/<build>/` of the GEO/SRA store, except the last two rows, which are in the paper
store root.

| File | One row per | Columns used |
| --- | --- | --- |
| `records/geo-series.parquet` | Series | `accession`, `published`, and `fields`: the lines `Series_title`, `Series_summary`, `Series_overall_design`, `Series_type`, `Series_sample_taxid` |
| `records/geo-sample.parquet` | Sample | `accession`, and `fields`: `Sample_library_strategy`, `Sample_library_source` |
| `records/sra-run.parquet` | run | `accession`, and `fields`: `RUN@size`, `RUN@total_bases`, `RUN@total_spots` |
| `links_by_from.parquet` | link, once for each source that states it | `from`, `to`, `relation`, `source` |
| `papers.parquet` | paper | `paper`, `pmid`, `doi`, `status` |
| `paper_info.parquet` | paper | `paper`, `abstract` |

`fields` is a list of `{name, value}` pairs: the source's own field names, in source order, with
repeats kept (read: `src/labdata/store/layout.py`). **`papers.parquet` on disk has no `other_dois`
column**, though liulab-data's current code declares one (read: the file's columns, at the run).

The link relations followed, each stored once under its forward name: `sample` (Series → Sample),
`SRA` (Sample → experiment), `run` (experiment → run), `SuperSeries of` (Series → Series),
`BioProject` (Series → BioProject), `umbrella of` (BioProject → BioProject), `PubMed` and `DOI`
(Series or BioProject → paper), and `Reanalyzed by` (Series → Series; counted, not a tie).

## What the store does not have

- **No DOI links in this build** (read: the build's commit comes before the commit that added the
  `DOI` relation; measured: 0 DOI keys among the linked papers). The live build gets them at
  December's refresh. Until then a Series linked to a paper only by DOI reads as having no paper.
  liulab-data counted 1,386 such target Series store-wide on this build
  ([liulab-data#207](https://github.com/liuhlab/liulab-data/issues/207)).
- **Single-cell is only partly a field.** `Sample_library_source` says `transcriptomic single
  cell` or `genomic single cell` for 1,784 candidate Series, while a text hint in the Series
  record finds 3,096 (measured). No field names a chemistry.
- **No FASTQ size as one field.** `RUN@size` is the size of the run's SRA archive, not of its
  FASTQ files; in the one run compared, the archive was about two thirds of the submitted
  `fastq.gz` (read: a liulab-data test fixture). Every size below is `RUN@size`.
- **No organism name on a Series.** The Series carries `Series_sample_taxid`, which is enough for
  *C. elegans* (6239).
- **Not only sequencing Series.** The build holds 6,982 more Series records than target Series:
  the Series that target Series link to. 184 candidates name no sequencing type at all, mostly
  array Series (measured).

## Method

The script is [`prototypes/standard_set/measure_candidates.py`](https://github.com/liuhlab/seqforge/blob/task/583-store-candidates/prototypes/standard_set/measure_candidates.py), kept on the branch `task/583-store-candidates` and not merged.
It reads both stores, writes nothing in them, and prints JSON lines: the run's provenance, one
line per candidate Series, per linked group and per linked paper, and a summary. It writes
accessions, typed fields and counts, and no record text.

**A candidate** is a held Series that fits a direction:

| Direction | Rule | Weakness |
| --- | --- | --- |
| *C. elegans* | a `Series_sample_taxid` line is 6239 | Includes Series of several organisms: 79 of the 3,338. |
| aging | a term list matches the title, summary or overall design, without regard to case: aging, ageing, lifespan, longevity, healthspan, senescence, elderly, rejuvenation, progeria, "age-related" and the like | A text match is not a topic. "Senescence" brings in cancer studies. "Aged" is left out because it matches "mice aged 8 weeks", so some true aging Series are missed. |
| neurodegeneration | a term list the same way: neurodegeneration, Alzheimer, Parkinson, Huntington, dementia, frontotemporal, tauopathy, prion and the like, plus the acronyms ALS, FTD and FTLD matched with case | "Prion" brings in yeast prion-domain work. AD and PD are left out as too ambiguous. Multiple sclerosis is left out. |

The term lists are in the script, and each run prints them in its provenance line. The text rules
size the pool. They do not pick a study: the drawing reads each one.

**A linked group** is a set of Series joined by any chain of these ties: a SuperSeries link, a
shared BioProject, BioProjects under one umbrella, or a shared paper. A group can hold Series that
fit no direction. Samples, experiments and runs are counted once per group, so a SuperSeries does
not count its SubSeries' samples twice.

## How it was run

One run on 2026-11-19, inside a Slurm job already held on a compute node of the cluster, with
8 DuckDB threads and a 16 GB memory limit. It took **31 s**: 13 s to scan the Series and read the
links and run sizes, 3 s for the Sample assays, and 15 s in Python to write the lines. The script went to the
node over standard input and the result came back over standard output, so nothing was written on
the cluster but DuckDB's spill folder in the node's own `/tmp`, which the script deletes:

```bash
ssh <node> 'cd /tmp && <liulab-data checkout>/.pixi/envs/default/bin/python -B - \
    --store <datalake>/geo-sra-store --papers <datalake>/paper-store \
    --tmp /tmp --threads 8 --memory 16GB' \
  < prototypes/standard_set/measure_candidates.py > <a folder outside this repo>/candidates.jsonl
```

`<node>` is a compute node the user holds a job on, never the login node. The run used DuckDB
1.5.6. The output stays outside this repo; every number below was counted again from it.

## Results

### Candidates by direction, and their overlap

15,879 of the 207,113 Series records fit a direction.

| | aging | neurodegeneration | *C. elegans* |
| --- | --- | --- | --- |
| Series | 9,257 | 4,586 | 3,338 |
| with the term in the title | 4,173 | 2,223 | – |
| with at least one experiment | 8,965 | 4,447 | 2,095 |
| SuperSeries / SubSeries | 399 / 2,259 | 186 / 952 | 157 / 414 |

Overlap: 880 fit aging and neurodegeneration, 355 aging and *C. elegans*, 17 neurodegeneration and
*C. elegans*, and 25 all three. Fewer than half of the text matches are in the title, so the two
text pools are loose.

**1,243 of the 3,338 *C. elegans* Series have no experiment, and 1,164 carry a re-analysis link.
They are one block, not worm studies of the usual kind.** All 1,164 are among the 1,243. They are
typed genome binding/occupancy profiling, none has an SRA experiment, 1,109 hold one or two
ChIP-Seq Samples, and all but one were published in 2024. 1,162 of them sit in one BioProject and
cite one paper, PMID 22955616. From memory, not checked here, that is the ENCODE consortium's
2012 paper, so the block looks like a consortium's bulk deposit of records whose reads live
elsewhere. The other 79 without an experiment are a mix, old array Series among them. Of the
1,794 *C. elegans* Series outside the 20,338-Series group described below, 1,751 have
experiments.

### Samples, experiments and bytes of reads per Series

Percentiles over the candidate Series of each direction. Read sizes are over the Series that have
a sized run, and are SRA archive sizes.

| | 10th | median | 90th | 99th | largest |
| --- | --- | --- | --- | --- | --- |
| aging: samples | 4 | 12 | 53 | 518 | 12,288 |
| aging: experiments | 3 | 12 | 49 | 479 | 12,288 |
| aging: reads, GB (8,876 Series) | 5.8 | 32.0 | 224 | 1,292 | 24,371 |
| neurodegeneration: samples | 4 | 13 | 64 | 426 | 15,360 |
| neurodegeneration: experiments | 4 | 12 | 60 | 422 | 15,360 |
| neurodegeneration: reads, GB (4,414 Series) | 6.0 | 39.8 | 276 | 1,387 | 24,371 |
| *C. elegans*: samples | 1 | 4 | 30 | 113 | 2,281 |
| *C. elegans*: experiments | 0 | 4 | 29 | 113 | 2,281 |
| *C. elegans*: reads, GB (2,092 Series) | 0.5 | 12.8 | 68.8 | 318 | 2,070 |

Outside the 20,338-Series group, the *C. elegans* median is 12 samples, 12 experiments and
16.5 GB, close to the other two directions. 584 candidate Series have 100 or more experiments, and 249 have 200 or
more. All candidates together hold 1,517 TB. 462 of their 773,007 runs have no size.

### Linked groups: size, and what ties them

The candidates fall in 10,399 linked groups.

| Series in the group | Groups |
| --- | --- |
| 1 | 8,164 |
| 2 | 639 |
| 3 to 5 | 1,344 |
| 6 to 20 | 245 |
| more than 20 | 7 |

What ties the 2,235 groups of more than one Series:

| Ties | Groups |
| --- | --- |
| SuperSeries, and a shared paper | 1,083 |
| a shared paper only | 889 |
| SuperSeries, no shared paper | 222 |
| a shared BioProject, with or without the others | 40 |
| an umbrella BioProject only | 1 |

A SuperSeries' BioProject is the umbrella of its SubSeries' BioProjects, so the umbrella tie
comes with nearly every SuperSeries and is folded into those rows. 210 of the 2,235 groups link no
paper. Over the 10,128 groups with a sized run, the median group has 15 samples, 15 experiments
and 39 GB; the 90th percentile is 72 samples, 70 experiments and 279 GB.

**Two groups are glue, not studies.**

- **20,338 Series in one group.** PMID 22955616 is linked from 19,401 Series, and four BioProjects
  hold 1,587 of the group's 1,602 candidates. The group has 46,528 samples and 160 TB. It holds
  the *C. elegans* block above.
- **682 Series in another**, with 479 papers, 28,034 samples and 80 TB. The output cannot name its
  glue. Its 26 candidates sit in 26 different BioProjects, and the 27 papers they link are each
  linked from at most five Series; the output counts no Series for the other 452 papers. A hub
  among those papers, an umbrella BioProject over many projects, or a chain of shared papers
  would each explain it (inferred).

The next largest groups hold 39, 36, 28, 24 and 23 Series. So a hub paper or an umbrella project
can make a group that is no study, and a rule that picks by group has to cap the group's size or
drop ties through a hub.

### One Series with one paper, or with none

| One-Series groups | all | aging | neurodegeneration | *C. elegans* |
| --- | --- | --- | --- | --- |
| all | 8,164 | 5,209 | 2,721 | 1,064 |
| one paper | 5,300 | 3,279 | 1,855 | 698 |
| no paper | 2,658 | 1,814 | 782 | 333 |
| two or more papers | 206 | 116 | 84 | 33 |

Over all 15,879 candidates, whatever their group: 11,425 link one paper, 3,703 none, 677 two and
74 three or more. **"No paper" is an upper bound on this build**, which holds no DOI links.

### Linked papers with an abstract in liulab-data

The candidates link 8,155 papers, all by PMID. The paper store holds 8,154 of them, and **8,117
(99.5%) have an abstract**. By status: 7,374 fetched, 557 only a person can save, 219 waiting on a
source, 4 skipped. Of the 12,176 candidates that link a paper, 12,142 have at least one paper with
an abstract.

### Assay mix

Series whose Samples all share one library strategy and source, the largest rows:

| Strategy / source | all | aging | neurodegeneration | *C. elegans* |
| --- | --- | --- | --- | --- |
| RNA-Seq / transcriptomic | 8,926 | 5,818 | 2,937 | 1,087 |
| ChIP-Seq / genomic | 2,274 | 421 | 209 | 1,698 |
| RNA-Seq / transcriptomic single cell | 1,392 | 926 | 553 | 15 |
| OTHER / genomic | 347 | 278 | 73 | 31 |
| ATAC-seq / genomic | 308 | 244 | 60 | 18 |
| miRNA-Seq / transcriptomic | 271 | 133 | 98 | 59 |
| Bisulfite-Seq / genomic | 224 | 185 | 56 | 1 |
| ncRNA-Seq / transcriptomic | 167 | 60 | 43 | 82 |

1,320 candidates mix two or more of these, most often ChIP-Seq with RNA-Seq (140). The ChIP-Seq
lead in *C. elegans* is the block of 1,164 records above. By Samples, RNA-Seq / transcriptomic is
364,176 of the 537,863 Sample counts, then OTHER / genomic 38,069, single-cell RNA-Seq 28,740 and
ChIP-Seq 26,371. A text hint of single-cell work is in 2,114 aging, 1,156 neurodegeneration and
66 *C. elegans* Series.
