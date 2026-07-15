---
name: seqforge-orchestrate
description: >-
  Owns the seqforge state machine: drive a dataset from FASTQ + metadata to a
  validated manifest and a runnable Snakemake config. Use whenever the task is
  "process this dataset", "compile this accession", "reprocess these runs", or
  when resuming a half-finished seqforge run. Delegates every stage to a
  deterministic CLI verb and never touches files directly. Read this BEFORE
  running any seqforge command, since it owns the order and the exit-code
  contract.
---

# seqforge orchestrate

You own the state machine and nothing else. **Every decision belongs to the CLI**; your job is to
call verbs in order, read exit codes, and route ambiguity to a human.

## The one thing to understand

seqforge is a **compiler, not a chatbot**. You are not being asked what chemistry this is — code
decides that from bytes. You are being asked to run the stages in order and to stop when code says
stop. If you ever find yourself reasoning about what the data probably is, you have left your lane.

## The pipeline

```bash
seqforge io resolve ACC --json          # optional: accession -> runs (+ dropped-read check)
seqforge probe FILES --json             # bytes -> Observation      (no LLM, no network)
seqforge harvest normalize DOCS         # prose -> canonical text
seqforge harvest extract DOCS --verify  # THE one LLM touchpoint    (delegate to seqforge-harvest)
seqforge resolve score FILES --json     # Obs + KB -> decision      (no LLM)
seqforge manifest fill ... && seqforge manifest validate MANIFEST
seqforge compose MANIFEST --json
```

`seqforge run` (alias `compile`) does all of it headlessly; `--no-llm` makes it fully deterministic;
`--resume` picks up after a kill (disk is state — R7).

## Exit codes are the contract (R4)

| code | meaning | what you do |
|---|---|---|
| 0 | OK | continue |
| 1 | ERROR (a bug or IO, not a domain refusal) | report it; do not retry blindly |
| 2 | USAGE | fix the command |
| 3 | BLOCKED — a `Blocker` | **stop.** Read `remedy`; it is actionable by contract |
| 4 | NEEDS_HUMAN — open `Conflict`/question | **stop and ask the human.** Never pick |

**Exit 3 and 4 are answers, not failures.** A refusal is the system working. Retrying a 3 with
different flags, or resolving a 4 yourself, converts a correct refusal into a wrong manifest — the
one failure this project exists to prevent, because nothing downstream ever questions it again.

## Rules you cannot bend

- **Never** pick between conflicting values yourself. Code decides or a human decides.
- **Never** read a whole FASTQ. Use `seqforge probe`; a hook blocks the alternatives (R3).
- **Never** write an absolute path into a manifest (R9). A hook blocks that too.
- **Never** touch a held-out acceptance case. It runs once, pre-registered, on the maintainer's word.

## Context hygiene

Delegate `probe` to **seqforge-exam** and extraction to **seqforge-harvest** as subagents: both
produce bulky output, and you should see a compact object, never raw FASTQ lines. If you are reading
sequence, something has gone wrong.
