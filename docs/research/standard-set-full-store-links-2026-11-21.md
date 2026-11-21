# What the full store links to each of the 50 studies

Dated 2026-11-21, for [#615](https://github.com/liuhlab/seqforge/issues/615), on build
`20261107T163933Z` of liulab-data's full GEO/SRA store. **For all 50 studies of the standard set,
the full store links nothing beyond the group the pick recorded. One hop over every kind of link
gathers exactly that group, and two hops or a full closure add nothing. Each kind of link alone
misses some studies' Series. All 56 linked papers have an abstract, and 51 have a full text.**

The question: the toy store was cut to the pick's groups, so a job that gathers links from it can
only find those groups again. Does the full store link more, and which links does a job have to
follow to gather a study's group?

Evidence class: everything here is **measured** by one job on arc, run on 2026-11-21. It read the
50 Series named under `evals/standard-set/` and their neighbours, and nothing else in GEO.

## How the pick computed its groups

The pick wrote each study's `linked_series` in
[`docs/research/standard-set-candidates.md`](standard-set-candidates.md) ("A linked group"), with
`prototypes/standard_set/measure_candidates.py` on tag `archive/task-583-store-candidates`. That
script joins Series into connected groups over every chain of links, with no hop limit and no cap.
A SuperSeries, a Series's BioProject, an umbrella BioProject and a paper that a Series links each
tie Series together. A paper that only a BioProject links ties nothing. Small placeholder PubMed IDs
that only BioProject records carry are dropped. `Reanalyzed by` is counted but ties nothing. Its
`linked_series` is the whole group on the build it read.

## Method

The script `scripts/research/615/gather_linked_groups.py` on tag
`archive/task-615-gather-linked-groups` follows four kinds of link out of each study's Series:

- **superseries:** a SuperSeries and its sub-Series.
- **bioproject:** a Series and its BioProject.
- **umbrella:** a BioProject and the umbrella BioProject above it.
- **paper:** a Series or a BioProject and a paper, by PubMed ID or DOI. This build holds no DOI
  links.

It runs eleven rules: each kind alone at one hop and at two, all four at one hop and at two, and
all four to closure. A paper, BioProject or SuperSeries linked from more than 200 Series is counted
but not followed. No hop starts from more than 2,000 Series, and closure stops at 10 hops. No study
reached any of these caps.

The job ran on the `cpu` partition with 4 CPUs and 16 GB requested. It cloned the script's commit
and read the store through liulab-data's own readers. `<work>` is a scratch folder and `<datalake>`
the folder that holds both stores:

```bash
export WORK=<work> DATALAKE=<datalake> LIULAB_DATA=<liulab-data checkout>
sbatch --chdir="$WORK" --output="$WORK/sbatch/%x.%j.out" --error="$WORK/sbatch/%x.%j.err" \
  scripts/research/615/gather_linked_groups.sbatch
```

| Read | Value |
| --- | --- |
| GEO/SRA store build | `20261107T163933Z`, made by `labdata store build` at liulab-data `e6962a5` |
| liulab-data checkout that read it | `720bd3c` |
| Paper store | last daily pass ended 2026-11-21 00:36 UTC; last PMC sync 2026-11-09 |
| seqforge commit | `5eec3cb` |
| Wall time | 14 s for the job, 9 s for the script, 344 lookups and 56 papers |
| Peak memory | 0.84 GB, Slurm's figure for the batch step |

## Result

`same` counts studies whose gathered Series equal the pick's `linked_series`. `misses` counts
studies where the rule did not reach a Series the pick has. No rule added a Series or a paper to
any study.

| Rule | Same | Misses | Series per study (median, max) | Papers per study (median, max) | Largest fan-out |
| --- | --- | --- | --- | --- | --- |
| superseries, 1 hop | 43 | 7 | 1, 3 | 1, 4 | 2 |
| superseries, 2 hops | 43 | 7 | 1, 3 | 1, 4 | 2 |
| bioproject, 1 hop | 40 | 10 | 1, 1 | 1, 4 | 1 |
| bioproject, 2 hops | 40 | 10 | 1, 1 | 1, 4 | 1 |
| umbrella, 1 hop | 43 | 7 | 1, 3 | 1, 4 | 3 |
| umbrella, 2 hops | 43 | 7 | 1, 3 | 1, 4 | 3 |
| paper, 1 hop | 49 | 1 | 1, 3 | 1, 4 | 3 |
| paper, 2 hops | 49 | 1 | 1, 3 | 1, 4 | 3 |
| all four, 1 hop | 50 | 0 | 1, 3 | 1, 4 | 3 |
| all four, 2 hops | 50 | 0 | 1, 3 | 1, 4 | 3 |
| all four, closure | 50 | 0 | 1, 3 | 1, 4 | 3 |

- The one study that paper links alone miss is GSE240375: its sub-Series GSE240373 and GSE240374
  are reached only through the SuperSeries.
- No paper, BioProject or SuperSeries met is linked from more than 3 Series.
- None of the 50 has a `Reanalyzed by` link.
- Of the 56 papers linked to the 50, all are in the paper store with an abstract, and 51 have a
  full text (XML or PDF on disk).

## What this does and does not show

- On these 50, a job that follows every kind of link one hop gathers the same group as a full
  closure. The 50 were picked to be small, so this says nothing about a large Series, a busy
  SuperSeries or a paper linked from many Series. The caps above are untested.
- The full store holds nothing for these 50 that the toy store lacks, so a job built and tried on
  the toy store sees the same groups it would see on the full store.
- The per-study output, `results.json` and `summary.tsv`, stays in the job's work folder on arc and
  is not committed.
