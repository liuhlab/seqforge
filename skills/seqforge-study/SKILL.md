---
name: seqforge-study
description: >-
  Run the study chain with `seqforge study run`: bound each GEO study (which
  Series and papers belong to it), then read what each sample was, into a folder
  of study folders. Use when asked to bound or read studies, check a bounds or
  reading file, or render a study's report. Every run spends money on models,
  so it always passes a dollar ceiling.
---

# seqforge study

A model draws each study's bounds and writes its reading; code checks both, and the files are
written only when they pass. You call the verbs and relay what they print.

| Action | Verb |
|---|---|
| Bound and read the studies of a folder | `seqforge study run --studies DIR --ceiling-usd USD` |
| Only some studies | `seqforge study run --studies DIR --keys FILE --ceiling-usd USD` |
| Check one bounds file | `seqforge study check-bounds DIR/ID/bounds.yaml` |
| Check one reading file | `seqforge study check-reading DIR/ID/reading.yaml` |
| See one study's bounds and reading | `seqforge report --study DIR/ID` |

`study run` runs the bounding pass, writes `bounds.yaml`, runs the reading pass, and writes
`reading.yaml`, in that order. Without `--keys` it runs the studies the folder's `pick.yaml` lists.
A file already on disk is kept, never overwritten. `--model ROLE=PROVIDER/NAME` takes the
`pass start` syntax and goes to each pass whose job has that role.

## The ceiling

`--ceiling-usd` is required and covers both passes: bounding spends first, and reading gets what is
left. Pass the ceiling the user named. With none named, ask for one before the first run. Raise a
ceiling only when the user asks: a run stopped at its ceiling is the ceiling working.

## Exit codes

| Code | Meaning | What you do |
|---|---|---|
| 0 | Every step ended | Report each step's counts from `steps` |
| 2 | A setup it cannot use: a bad option, no gateway, a store it cannot open | Fix what the `error` names |
| 3 | Stopped: at the ceiling, a provider stop, a held task, or a file the format check rejects | Report the step's `stopped` and `message`, then stop |

The first step that does not exit 0 stops the chain, and `steps` ends at it. Run the same command
again to resume: every unit already answered replays from the gateway's cache at no cost, so a
rerun spends only on the units not yet answered.

A study can end `refused` or `unsettled` while the verb exits 0. A refused study gets no file, and
its reason is in the write step's `studies`. Relay those as they are written.
