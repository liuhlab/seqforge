# Read your own studies

This guide takes a list of GEO Series (studies in the Gene Expression Omnibus archive) and gives you
two files for each study, plus a report page:

- `bounds.yaml` says which Series and which papers belong to the study, and which linked records
  were left out and why.
- `reading.yaml` says what each sequenced sample was: its species, tissue or cell type, condition,
  and the factors the study varied on purpose, such as a treatment or an age. Each statement
  quotes the record or paper it came from, or says that it was inferred or unknown.

Models draw the bounds and write the reading. Code checks each quote against its source and each
file against its format before it writes the file. To see what the result looks like, open the
[example reports](../examples/index.md). [Understanding a study](../how-it-works/study.md) explains
how the two models work and why a study can end unsettled.

## Before you start

You need seqforge with `liulab-data` installed (see [Getting started](../getting-started.md)), and
these four settings in your shell:

| Variable | What it points at | Needed for |
| --- | --- | --- |
| `LABLLM_GATEWAY` | The lab's model gateway, as `http://host:port`. Open a tunnel to it first | Every model question. Without it the run stops before it spends anything |
| `LABDATA_STORE` | `liulab-data`'s record store: its local copy of GEO and SRA records | Finding each study's linked Series, samples and papers |
| `LABDATA_PAPERS` | `liulab-data`'s paper store | Reading a study's papers. A study that has a paper waits until this is set |
| `LABDATA_ONTOLOGIES` | `liulab-data`'s ontology store: dated lists of terms for species, tissues, cell types and conditions | Naming each value in the reading by an ontology term |

If you saved a store's location with `liulab-data` itself, you can leave its variable unset. [Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md)
says how to open the tunnel.

## Set up a folder of studies

Make an empty folder for the studies. seqforge makes one folder in it for each study, named by its
Series id. List the studies in one of two ways.

**A `pick.yaml` in the folder.** It needs a `studies` list, with the Series id of each study under
`id`:

```yaml
studies:
  - id: GSE100021
  - id: GSE126954
  - id: GSE140511
```

**A keys file.** Write one Series id on each line, and pass the file with `--keys`. A keys file
overrides the folder's `pick.yaml`, and the run takes the studies in the file's order.

```text
GSE100021
GSE126954
```

Each id must be a GEO Series, `GSE` and a number. Start from the Series the study was published
under. The bounding job finds the other Series and papers linked to it.

## Run it under a spending limit

Pick one working folder and run every command from it. seqforge keeps the record of each pass in
`seqforge/passes/` under the folder you run from, and a rerun from the same place resumes it.

```bash
seqforge study run --studies <folder> --ceiling-usd 2
```

`--ceiling-usd` is required and has no default. It is the most both jobs together may spend, in
dollars. The bounding job spends first, and the reading job gets what is left. To run only some
studies, add `--keys <file>`.

The command runs four steps in order:

1. The bounding job asks a model about each linked record.
2. seqforge writes `<folder>/<ID>/bounds.yaml` for each study.
3. The reading job reads the records and papers that the bounds kept.
4. seqforge writes `<folder>/<ID>/reading.yaml` for each study.

It prints one JSON object. `spent_usd` is what the run paid, and `steps` holds what each step
printed. The first step that fails stops the run.

### What a run costs

These numbers come from a run of the 50 studies of the standard set, the lab's fixed list of small
GEO studies, on 2026-11-27. The bounding job asks Jev, a model that answers yes-or-no and
multiple-choice questions with a probability. The reading job asks Jev and a writer model, DeepSeek
Flash, which lists each study's factors and values. To use another model for one of these roles,
pass `--model`.

| Job | Settled | Unsettled | Refused | First run of the 50 |
| --- | --- | --- | --- | --- |
| Bounding | 47 | 3 | 0 | $0.0104 |
| Reading | 38 | 12 | 0 | $1.537, about 3 cents a study |

A first reading of the 50, with nothing in the cache, took about 22 minutes at four studies at
once (`--units-at-once 4`; the default is 32). The gateway keeps every answer, so the run of
2026-11-27 replayed them all and paid $0.00. The full measurement is in the
[dated note on this run](https://github.com/liuhlab/seqforge/blob/main/docs/research/study-chain-on-the-50-2026-11-27.md).

## Read how each study ended

Each job ends each study in one of three ways.

| Ending | What it means | File written |
| --- | --- | --- |
| Settled | Every check passed. | Yes |
| Unsettled | The file passed its checks, but Jev was asked a question twice, in two forms, and the two answers did not agree. A person or a stronger model should look at it | Yes |
| Refused | The job could not give an answer it could check. The reason says why, such as a group of more than 50 linked Series, or a writer answer that could not be used twice | No |

For bounding, Jev is asked about each record twice: as written, and negated. For reading, Jev is
asked twice whether each factor was varied on purpose, which level is the baseline, and each
sample's level. Any disagreement makes the study unsettled.

The ending is kept in the pass's record, not in the study's files. You can find it in two places:

- In the JSON that `study run` prints, the two write steps list each study under `studies`, with
  its `ending`. A refused study has its `reason` there.
- `seqforge pass status study-bounds` and `seqforge pass status study-reading` print the count of
  each ending. Run them from the same working folder. They work while a pass runs and with the
  gateway down.

A study can also wait without an ending. That happens when its id is not a GEO Series, when the
record store does not hold it, when it has a paper and no paper store is set, or, for the reading,
when its `bounds.yaml` is missing or fails its check. `pass status`
lists each waiting study under `not_done`, with the reason. Fix the reason, then run again.

## Resume a run that stopped

Run the same command again. A study that was already answered replays from the gateway's cache and
costs nothing, so the rerun pays only for studies not yet answered.

The exit code says why a run stopped:

| Code | Meaning |
| --- | --- |
| `0` | Every step finished. Some studies may still be unsettled or refused |
| `2` | It could not start, such as no gateway or a store it cannot open. Nothing was spent |
| `3` | It stopped: at the spending limit, at a provider's limit, while another process held the pass, or at a file that failed its format check |

A file already on disk is never replaced. To take a newer answer for a study, delete its file
first, then run again.

## Check a file

Each file has a format, and a check that seqforge runs before it writes the file. You can run the
same check on any file, for example one you edited by hand:

```bash
seqforge study check-bounds <folder>/<ID>/bounds.yaml
seqforge study check-reading <folder>/<ID>/reading.yaml
```

Each prints `valid` and a list of `problems`, each naming a field and the reason. The exit code is
0 for a valid file, 3 for a file with problems, and 2 for a file it cannot read. `check-reading`
needs `liulab-data` installed, because its term lists decide whether each ontology id is real. It
checks the file alone, so it does not look the quotes up in their records. The format pages list
every field: [the bounds file](../formats/bounds.md) and [the reading file](../formats/reading.md).

## Make a report for a study

```bash
seqforge report --study <folder>/<ID>
```

This writes `report.html` and `report.json` into the study's folder. The page opens in any browser
with no network. It shows the bounds as a graph of the records kept and left out, and the reading's
samples in tables by topic. It does not show whether the job ended the study settled or unsettled.
That is in the pass's record, as described above.

To join a reading to a compiled dataset, pass the same study folder to `seqforge run --study` when
you compile. [From FASTQ files to a pipeline](../how-it-works/compiler.md) explains the join.

## Go further

- [The study workflow in detail](../study-workflow.md) gives each step and the rules it follows.
- [Passes](../passes.md) covers running one job at a time, its spending limit, and what a pass
  prints.
