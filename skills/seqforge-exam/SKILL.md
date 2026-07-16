---
name: seqforge-exam
description: >-
  Fingerprint FASTQ files into a structural Observation using `seqforge probe`
  (and `seqforge io peek` for remote files). Use when asked to inspect, examine,
  characterise, or "look at" sequencing reads — what read lengths, is there a
  barcode, is the gzip intact, how many reads. Returns a compact object, never
  raw sequence. Run as a subagent for context hygiene.
---

# seqforge exam

Turn bytes into an `Observation`. Deterministic: no LLM, no network, no interpretation.

```bash
seqforge probe FILES...            # local; bounded by construction
seqforge io peek URI               # remote; HTTP Range, ~64 KB, never the file
```

## The budget is not negotiable

`probe` stops at **200 000 reads AND 256 MB decompressed**, whichever comes first. Wall-clock is
never a budget. A 40 GB FASTQ costs the same as a 40 MB one, and a code path that *can* stream a
whole file is a bug, not a risk to manage. Do not work around this with `zcat`/`head` — a hook will
block it, and it should.

## What you return

A compact object: read lengths, per-cycle composition, distinct-value windows, gzip integrity, read
count estimate, header grammar. **Never raw sequence.** If a caller needs an example, quote one short
read — the orchestrator should never see a FASTQ line.

## What you must NOT do

The `Observation` is **role-free by design**: it says "read 1 is 28 bp with a low-diversity 16 bp
window", never "read 1 is the barcode read". Naming roles is `resolve`'s job, scored against the KB.
Volunteering "this looks like 10x v3" skips the scoring engine and re-introduces the guess the whole
architecture removes — you would usually be right, which is exactly what makes it dangerous.

Report what the bytes say. Nothing else.
