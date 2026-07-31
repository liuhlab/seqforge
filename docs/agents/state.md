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
| `logs/` | `logs/usage.json` (the harvest token/mode cost ledger) and `logs/assertions.json` — run and debug material, never the deliverable | yes |

`manifest.yaml` is written only after a clean `manifest validate`, and exactly one of
`manifest.yaml` / `manifest.draft.yaml` exists at a time (`fill` unlinks the other).

Compiled output lives under `pipeline/<recipe>-<run_id[:12]>/` — `config.yaml`, `units.tsv`,
`Snakefile`, `processing.lock.yaml`, and a **copy of the hand-written module** the wrapper imports
locally. Keyed by the **run**, because one dataset compiled two ways is two runs
([ADR-0005](../adr/0005-run-id-is-the-pairing.md)). The module is copied in rather than referenced by
package path so that a run directory still reads and reproduces after it is moved off the composing
machine.

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
`journal.jsonl`, `distill`, `LESSONS.md`. Its design survives in `docs/design.md` §9.
