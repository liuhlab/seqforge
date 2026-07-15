---
name: seqforge-journal
description: >-
  Append decisions to the append-only journal and distil recurring lessons with
  `seqforge journal append|show|distill`, plus `seqforge status`. Use when
  recording why a decision was made, reviewing what happened to a dataset, or
  turning repeated findings into LESSONS.md.
---

# seqforge journal

```bash
seqforge journal append --event ... --json
seqforge journal show [--dataset ID]
seqforge journal distill          # journal.jsonl -> a LESSONS.md draft
seqforge status                   # what stage is this dataset at?
```

## Disk is state, context is cache (R7)

`journal.jsonl` is **append-only**. Never rewrite history to make it tidy: a journal you edit is a
story, and the point of this file is that it is a record. Every stage writes a resumable,
content-addressed artifact under `.seqforge/`, so any run survives a kill and the agent never holds
state only in context.

## Distil is a draft, not a decision

`distill` proposes `LESSONS.md` entries; **a human approves them.** The verb is deterministic; the
drafting is yours. A lesson that promotes itself is just an opinion with better formatting.

Worth recording, because it repeats:

- a `Conflict` that turned out to be a *metadata* error, not a data one
- a refusal (exit 3/4) that was **correct** — those are the cheapest lessons and the easiest to
  mistake for failures
- a case where a guess would have landed. Those are the dangerous ones: being right by luck is
  indistinguishable from knowing, right up until it isn't
