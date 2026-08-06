# On-disk state: `seqforge/`, resumable and content-addressed

R5 in one place. One owner for the name: [`workspace.py`](../../src/seqforge/workspace.py) — it is the
only file that spells `seqforge/`, and the only one that knows a readable name is a stem plus a
12-char hash.

The top level carries only what a human reaches for — the manifest, the project views, `pipeline/` —
and everything else sorts into one of three subtrees:

| subtree | holds | safe to delete |
|---------|-------|----------------|
| `cache/` | `cache/observations/` (per-file `Observation`, keyed by content-address: a bounded local key, or a provider md5 for hosted bytes — **never** a whole-file sha256); `cache/candidates/` (per-dataset, keyed by `sha256(sorted(file_shas) ⊕ kb_version)` with probe/resolve versions folded in); `cache/taxonomy.json` | yes |
| `records/` | `records/<accession>.json` — what the archive declared; `records/documents/<stem>-<hash12>.txt` — the canonical text a span greps into, including documents rendered from records, which live with the records they came from | no |
| `logs/` | `logs/usage.json` (the harvest token/mode cost ledger), `logs/transcript.jsonl` (the exchanges that ledger is about — one header line carrying the system prompt, then one line per `Exchange`) and `logs/assertions.json` — run and debug material, never the deliverable | no — rebuildable only by paying for the tokens again |

`manifest.yaml` is written only after a clean `manifest validate`, and exactly one of
`manifest.yaml` / `manifest.draft.yaml` exists at a time (`fill` unlinks the other).

Compiled output lives under `pipeline/<recipe>-<run_id[:12]>/` — `config.yaml`, `units.tsv`,
`Snakefile`, `processing.lock.yaml`, and a **copy of the hand-written module** the wrapper imports
locally. A sixth file, `excluded.md`, appears only when the chemistry declares a `min_input_reads`
floor **and** a sample fell below it: it is what reconciles a sample list shorter than the manifest's
with the dataset that still carries every one
([ADR-0032](../adr/0032-a-spec-declares-the-shape-of-a-deposit.md)).
`workspace.py` names that subtree like every other one; what is *inside* a run directory
belongs to [`pipeline.py`](../../src/seqforge/pipeline.py), which the composer writes through and the
report, the gates and the ground-truth harness read through
([ADR-0024](../adr/0024-one-owner-for-the-compiled-pipeline.md)). Keyed by the **run**, because one dataset compiled two ways is two runs
([ADR-0005](../adr/0005-run-id-is-the-pairing.md)). The module is copied in rather than referenced by
package path so that a run directory still reads and reproduces after it is moved off the composing
machine.

**What the pipeline itself writes lands under that directory too**, at the config's `outdir`
(`results/` by default), one subdirectory per sample. seqforge does not write a byte of it — the user
submits the Snakefile — but `seqforge report` reads it back: each **Workflow module** declares which
artifact name to look for and how to parse it
([ADR-0025](../adr/0025-the-module-that-writes-an-artifact-owns-reading-it.md)), and `report
--results` relocates the search for a pipeline run with `snakemake --directory`.

`eval/` is the fourth top-level thing, beside `pipeline/`, `fingerprint/` and `report.html`.
`seqforge eval run -C <root>` writes `eval/report.json` — byte-identical to what it prints — and
`eval/transcripts/<case>.jsonl` for each case that reached a model, and `seqforge eval report` is
handed that directory. Output rather than cache: re-running it costs real tokens. The directory is
also why the rendered page can show the chat history at all: `--transcript sample|all|none` reads
those files from **beside** the report, never out of it, and the sample states how many exchanges it
left out.

**Onlists are not stored.** `rule onlist` materializes a whitelist, STAR reads it, `temp()` deletes
it — expanding 6 794 880 barcodes into every run directory cost 111 MB of duplicate bytes
([ADR-0015](../adr/0015-onlists-are-built-and-deleted.md)).

## Hooks: policy as mechanism

Three hooks turn rules into things that cannot be forgotten, all behind `seqforge hook …`
(`hooks/guards.py`):

- **`PreToolUse`** blocks unbounded FASTQ streams (size-blind, so it cannot be argued out of by a
  small file) and `/scratch` or absolute-path writes — R3 and R7.
- **`PostToolUse`** auto-runs `manifest validate` after a manifest edit — R2.
- **`Stop`** refuses to end a turn while `questions.md` is non-empty. `fill` is what *writes* that
  file: `_sync_questions` (`cli/manifest.py`) renders the open conflicts and questions across runs,
  and clears a stale file when none remain.

One design-called-for piece of state is still **unbuilt**: the journal flywheel entirely —
`journal.jsonl`, `distill`, `LESSONS.md`. It is tracked as an open issue, with the rest of the scope
delta.
