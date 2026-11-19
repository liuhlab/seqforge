# The candidate studies for the standard set, on one build of liulab-data's store

Started 2026-11-19 for [#583](https://github.com/liuhlab/seqforge/issues/583). **The numbers are
not measured yet.** This note holds what was found about the store and the method that will
produce the numbers; each number section below says so until the run fills it.

The question: for sequencing Series that fit one of three directions (aging, neurodegeneration,
*C. elegans*), how big is each Series, what is it linked to, how many papers does it have, does
liulab-data hold each paper's abstract, and what is the assay mix?

Evidence classes: **read** means seen in liulab-data's code or docs, or in a file of the store on
2026-11-19. **Inferred** means reasoned from what was read and not checked. Nothing here was
queried: no table of the store has been opened yet.

Terms: a **Series** is a GEO Series (`GSE…`), a **Sample** a GEO Sample (`GSM…`). An
**experiment** is an SRA experiment (`SRX…`) and a **run** an SRA run (`SRR…`). A **target
Series** is a Series whose type names high-throughput sequencing; the store holds these and the
records they link to. A **build** is one set of the store's tables, named by the UTC time it
started.

## Where the store is and how to open it

| Item | Value | How known |
| --- | --- | --- |
| Cluster | the lab's GPU cluster, ssh alias `arc` | read: liulab-data's `store-maintenance` skill names it as where the held job runs |
| GEO/SRA store root | `<datalake>/geo-sra-store`, on the cluster's shared scratch | read: a listing of the folder on 2026-11-19 |
| Paper store root | `<datalake>/paper-store`, beside it | read: the per-user pointer file `~/.cache/liulab-data/papers.toml` on the cluster |
| Build | `20261107T163933Z`, switched to on 2026-11-07, 10.9 GB | read: `labdata-store.toml` and `README.md` in the store root |
| Made by | liulab-data commit `e6962a5` | read: the store's `README.md` |
| Snapshots it read | `geo-series`, `sra-accessions`, `bioproject` and `biosample`, all dated 2026-11-07 | read: the store's `README.md` |
| Target Series | 200,132 listed, 200,131 held | read: the build's `README.md`; counted by liulab-data, not here |
| Paper tables | written 2026-11-17 | read: file dates in the paper store root |
| On this Mac | no store, no pointer file, `LABDATA_STORE` unset | read 2026-11-19 |

`<datalake>` stands for the full path, which stays out of this repo. A later session finds it on
the cluster: the paper store's pointer file gives `<datalake>/paper-store`, and the GEO/SRA store
is the folder `geo-sra-store` beside it.

**The GEO/SRA store has no pointer file on the cluster, and `LABDATA_STORE` is not set in a
non-interactive shell there** (read). So `labdata.open_store()` with no argument fails; pass the
root. The paper store's pointer file exists, so `open_paper_store()` finds that one.

Google Cloud Storage holds the source of truth for both stores, and the cluster folder is the
work disk a refresh runs on (read: liulab-data `docs/how-to/store-gcs.md`). That page also gives a
way to read the store on another computer: copy the marker and the one build folder with `rclone`.
It was not tried; a download out of Google Cloud Storage costs money and needs the maintainer's
yes.

Two ways to read the tables (read: the store's `README.md`):

```python
from labdata import open_store                    # in liulab-data's pixi environment
store = open_store("<datalake>/geo-sra-store")
store.table("GEO Series", ["Series_title"], where="published >= '2024-01-01'")
```

```python
import duckdb                                     # or plain DuckDB over the build's files
duckdb.sql("SELECT count(*) FROM read_parquet('<store>/builds/<build>/records/geo-series.parquet')")
```

The store has no query command. On the cluster, liulab-data's checkout holds a pixi environment
named `default`, which has DuckDB (inferred: labdata depends on it; its version was not checked).

**liulab-data's own reader keeps DuckDB's spill files under `~/.cache/liulab-data/duckdb`** (read:
`src/labdata/store/reader.py`). On the cluster that is the home folder, which must not be written.
The measurement therefore uses plain DuckDB with its own spill folder.

## The tables the measurement reads

All in `builds/<build>/` of the GEO/SRA store, except the last two rows, which are in the paper
store root (read: `src/labdata/store/layout.py`, `src/labdata/papers/tables.py`, and the folder
listing).

| File | One row per | Columns used |
| --- | --- | --- |
| `records/geo-series.parquet` | Series | `accession`, `published`, and `fields`: the lines `Series_title`, `Series_summary`, `Series_overall_design`, `Series_type`, `Series_sample_taxid` |
| `records/geo-sample.parquet` | Sample | `accession`, and `fields`: `Sample_library_strategy`, `Sample_library_source` |
| `records/sra-run.parquet` | run | `accession`, and `fields`: `RUN@size`, `RUN@total_bases`, `RUN@total_spots` |
| `links_by_from.parquet` | link, once for each source that states it | `from`, `to`, `relation`, `source` |
| `papers.parquet` | paper | `paper`, `pmid`, `doi`, `other_dois`, `status` |
| `paper_info.parquet` | paper | `paper`, `abstract` |

`fields` is a list of `{name, value}` pairs: the source's own field names, in source order, with
repeats kept. Every record table also has `type`, `status`, `submitted`, `updated`, `raw_text`,
`content_hash` and `snapshot_date`, which the measurement does not read.

The link relations it follows, each stored once under its forward name:

| Relation | From → to | Used for |
| --- | --- | --- |
| `sample` | Series → Sample | samples per Series |
| `SRA` | Sample → experiment | experiments per Series |
| `run` | experiment → run | runs and read sizes |
| `SuperSeries of` | Series → Series | a tie |
| `BioProject` | Series → BioProject | a tie, when two Series share one |
| `umbrella of` | BioProject → BioProject | a tie, through the umbrella |
| `PubMed`, `DOI` | Series or BioProject → `PMID:<n>` or `DOI:<doi>` | papers; a tie when two Series share one |
| `Reanalyzed by` | Series → Series | counted, not a tie |

## What the store does not have

- **No DOI links in this build** (read: the build's commit comes before the commit that added the
  `DOI` relation). The live build gets them at December's refresh. Until then a Series linked to a
  paper only by DOI reads as having no paper. liulab-data counted 1,386 such target Series on this
  build ([liulab-data#207](https://github.com/liuhlab/liulab-data/issues/207)). The script counts
  DOI links when a build has them.
- **No column says single-cell, or names a chemistry.** `Sample_library_strategy` says `RNA-Seq`
  for bulk and single-cell alike. The script adds a weak text hint, `mentions_single_cell`.
- **No FASTQ size as one field.** `RUN@size` is the size of the run's SRA archive, not of its
  FASTQ files; in the one run compared, the archive was about two thirds of the submitted
  `fastq.gz` (read: a liulab-data test fixture). The script reports `RUN@size` as the bytes of
  reads, beside bases and spots.
- **No organism name on a Series.** The Series carries `Series_sample_taxid`; the name is on each
  Sample. The taxid is enough for *C. elegans* (6239).
- **Papers only for target Series and held BioProjects**, and an abstract only where PubMed or
  OpenAlex gave one (read: the paper store's `README.md`).

## Method

The script is [`prototypes/standard_set/measure_candidates.py`](../../prototypes/standard_set/measure_candidates.py).
It reads both stores, writes nothing in them, and prints JSON lines: one for the run's
provenance, one per candidate Series, one per linked group, one per linked paper and a summary.
It writes accessions, typed fields and counts, and no record text.

**A candidate** is a held target Series that fits a direction. Every Series in the store is
already a sequencing Series, because the store holds only target Series.

| Direction | Rule | Weakness |
| --- | --- | --- |
| *C. elegans* | a `Series_sample_taxid` line is 6239 | Includes a Series of several organisms; `n_taxids` shows them. |
| aging | a term list matches the title, summary or overall design, without regard to case: aging, ageing, lifespan, longevity, healthspan, senescence, elderly, rejuvenation, progeria, "age-related" and the like | A text match is not a topic. "Senescence" brings in cancer studies. "Aged" is left out because it matches "mice aged 8 weeks", so some true aging Series are missed. |
| neurodegeneration | a term list the same way: neurodegeneration, Alzheimer, Parkinson, Huntington, dementia, frontotemporal, tauopathy, prion and the like, plus the acronyms ALS, FTD and FTLD matched with case | "Prion" brings in yeast prion-domain work. AD and PD are left out as too ambiguous. Multiple sclerosis is left out. |

The term lists are in the script, and each run prints them in its provenance line. A match in the
title is counted apart from a match anywhere, as a rough guide to how many matches are on topic.
The text rules are a first cut for sizing the pool. They do not pick a study: the drawing reads
each one.

**A linked group** is a set of Series joined by any chain of these ties: a SuperSeries link, a
shared BioProject, BioProjects under one umbrella, or a shared paper. A group can hold Series that
fit no direction. Samples, experiments and runs are counted once per group, so a SuperSeries does
not count its SubSeries' samples twice. Known weakness: one paper or one umbrella BioProject
linked from very many Series joins them all into one group. The summary lists the 20 largest
groups so that such a hub shows.

Per candidate Series the script gives samples, experiments, runs, bytes of reads, bases, its
papers and how many have an abstract, its BioProjects, whether it is a SuperSeries or a SubSeries,
and its Samples by library strategy and source. Per group it gives the member Series, the ties and
the same counts. The summary gives the spread of each count overall and by direction, groups by
size and by ties, the groups of one Series with no paper, one paper or more, papers by status and
with an abstract, and the assay mix.

## How to run it

Not run yet. On a tiny made-up store the script runs and counts a SuperSeries' samples once
(tried 2026-11-19 on the Mac; no real record was in it).

The script goes to the node over standard input and the result comes back over standard output,
so no file is written on the cluster except DuckDB's spill folder in the node's own `/tmp`, which
the script deletes:

```bash
ssh <node> 'cd /tmp && <liulab-data checkout>/.pixi/envs/default/bin/python -B - \
    --store <datalake>/geo-sra-store --papers <datalake>/paper-store \
    --tmp /tmp --threads 8 --memory 16GB' \
  < prototypes/standard_set/measure_candidates.py > <a folder outside this repo>/candidates.jsonl
```

`<node>` is a compute node the user holds a Slurm job on, never the login node. Add `--limit 200`
for a first try on the 200 lowest-numbered candidates.
[`measure_candidates.sbatch`](../../prototypes/standard_set/measure_candidates.sbatch) wraps the
same command as a job of its own: 8 CPUs, 32 GB, 30 minutes.

Expected time: a few minutes (inferred). The script scans chosen columns of three record tables
that are 0.4 GB, 0.9 GB and 4.6 GB on disk, and reads the 0.1 GB link table four times.
liulab-data's benchmark query over the whole Sample table took 6.4 s on a lightly loaded node
(read: the note on the branch `research/labdata-inputs-for-a-study-reader`, quoting liulab-data's
run spec). Each step prints its own time to standard error, so the first run measures this.

**The cluster's shared scratch was full on 2026-11-19** (read: 8 GB free). Write nothing there.

## Results

Not yet measured. Fill each section from the `summary` line of one run, with that run's
`provenance` line beside it.

### Candidates by direction, and their overlap

Not yet measured.

### Samples, experiments and bytes of reads per Series

Not yet measured.

### Linked groups: size, and what ties them

Not yet measured.

### One Series with one paper, or with none

Not yet measured.

### Linked papers with an abstract in liulab-data

Not yet measured.

### Assay mix

Not yet measured.
