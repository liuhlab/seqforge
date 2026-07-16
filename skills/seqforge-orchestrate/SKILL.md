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
call verbs, read exit codes, and route ambiguity to a human.

## The one thing to understand

seqforge is a **compiler, not a chatbot**. You are not being asked what chemistry this is — code
decides that from bytes. You are not being asked which read is the barcode, what the organism is, or
what any prose "means". You are being asked to run the compiler and to **stop when code says stop**.
If you ever find yourself reasoning about what the data probably is, you have left your lane.

## Default: one pass with `seqforge run`

For "compile this dataset", reach for `run` first. It chains the whole pipeline — records, harvest,
manifest fill, processing new, compose — stops at the first refusal, and prints one JSON summary.

```bash
seqforge run FILES... \
    [--accession PRJNA...] [--doc paper.pdf] \
    --assembly ce11 --annotation WS298 \
    --fastq-dir DIR
```

- **`FILES...`** are the FASTQ `.gz` files. Everything else is optional except the genome.
- **`--accession`** pulls the archive's project/sample/experiment/run records (where `strain`,
  `tissue`, `sex`, `dev_stage` live). Omit it for data that never had one — most data.
- **`--doc`** hands a paper (or any `.pdf`/`.txt`/`.md`) to the one LLM stage, which turns prose into
  span-verified claims. This stage calls its **own** model provider (`DEEPSEEK_API_KEY` /
  `ANTHROPIC_API_KEY` in the environment) — it is not you. If no key is set, `run` exits 1 telling
  you so; add `--no-llm` to skip prose entirely and stay fully deterministic.
- **`--assembly`/`--annotation`** are the genome — **the one real decision**, and it has no default.
  A wrong genome aligns cleanly and exits 0, so seqforge refuses to guess. If the user named a genome
  in prose, that arrives as a verified instruction instead; otherwise pass the flags.
- **`--fastq-dir`** is where this machine keeps the FASTQs, so `units.tsv` can find them.

**Run it in the foreground — do NOT background it.** `seqforge run` is a single bounded command (R3
caps every FASTQ read; the probe reads ~10 MB of a 20 GB file, not the whole thing) and finishes in
roughly a minute or two, paper included. Let the command block and read its JSON summary when it
returns. Backgrounding it is the one reliable way to lose the whole run: in a headless (`-p`) session
there is no next turn to receive a background-completion notification, so a backgrounded pipeline is
**killed when the turn ends**, leaving a half-written workspace with only `records/`. If you catch
yourself starting the pipeline in the background and then "waiting for a notification", stop — the
notification is never coming. The same holds for the staged verbs: run each to completion in the
foreground. (It is not slow. The reputation that the probe takes many minutes predates the parallel,
vectorized resolver; `--cpus` defaults to using several cores.)

The summary's `snakefile` path is the deliverable. `run` writes `manifest.yaml`, `processing.yaml`,
and the pipeline directory under `seqforge/`; re-running is resumable through each stage's
content-addressed cache (R7) — **there is no `--resume` flag**, you just run it again.

`compile` is an alias for `run`.

## When you need the stages one at a time

Same order, when a step needs inspection or a re-run in isolation:

```bash
seqforge io records ACC           # accession -> project/sample/experiment/run records (network)
seqforge probe FILES              # bytes -> Observation      (no LLM, no network)
seqforge harvest extract DOCS --records seqforge/records/ACC.json   # the one LLM touchpoint
seqforge resolve score FILES      # Obs + KB -> library decision  (no LLM)
# --- the IR: what the data IS. One per dataset, immutable (R13). Takes no genome. ---
seqforge manifest fill FILES --accession ACC --assertions seqforge/assertions.json
seqforge manifest validate seqforge/manifest.yaml
# --- the flags: what to DO with it. Many per dataset. Optional — compose defaults them. ---
seqforge processing new seqforge/manifest.yaml --assembly ce11 --annotation WS298 -o seqforge/processing.yaml
seqforge compose seqforge/manifest.yaml --processing seqforge/processing.yaml --fastq-dir DIR
```

**Two artifacts, and the difference matters to you (R13).** `manifest.yaml` is what the data *is* —
immutable, content-addressed, one per dataset. `processing.yaml` is what to *do* with it, and there
may be many. Re-running a dataset a different way means a **new processing manifest**, never an edit
to `manifest.yaml`: same IR, different flags. If you catch yourself editing the dataset manifest to
change how something is processed, stop — that is the bug the split exists to prevent, and the
`dataset_hash` is what proves it did not happen.

## Exit codes are the contract (R4)

Whether you run `run` or the stages, the codes are the same, and they are how the compiler talks to
you:

| code | meaning | what you do |
|---|---|---|
| 0 | OK | continue |
| 1 | ERROR (a bug or IO — e.g. no LLM provider) | report it; do not retry blindly |
| 2 | USAGE (or a genome is missing) | fix the command; supply what it names |
| 3 | BLOCKED — a `Blocker` | **stop.** Read `remedy`; it is actionable by contract |
| 4 | NEEDS_HUMAN — open `Conflict`/question | **stop and ask the human.** Never pick |

**Exit 3 and 4 are answers, not failures.** A refusal is the system working. Retrying a 3 with
different flags, or resolving a 4 yourself, converts a correct refusal into a wrong manifest — the
one failure this project exists to prevent, because nothing downstream ever questions it again. When
`run` stops on a 3 or 4, report the failing stage's `error`/`blockers`/`conflicts` **verbatim** and
stop; do not work around it.

## Rules you cannot bend

- **Never** decide chemistry, read roles, the organism, or which sample is which yourself. Code
  decides from bytes and records; a human decides a genuine ambiguity.
- **Never** pick between conflicting values. Code decides or a human decides.
- **Never** read a whole FASTQ. Use `seqforge run`/`probe`; a hook blocks the alternatives (R3).
- **Never** write an absolute path into a manifest (R9). A hook blocks that too.

## Context hygiene

When you drive the stages by hand, delegate `probe` to **seqforge-exam** and extraction to
**seqforge-harvest** as subagents: both produce bulky output, and you should see a compact object,
never raw FASTQ lines. If you are reading sequence, something has gone wrong. `run` already keeps that
output on disk and hands you only the summary.
