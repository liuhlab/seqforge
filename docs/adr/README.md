# Architecture decision records

One decision per file: what was there, what the obvious alternative was, and why it was rejected.
[`docs/agents/rules.md`](../agents/rules.md) states what is enforced and points here; this tree holds
the argument, once.

Every record carries a **`## So in code`** section: the imperative a reader must follow, and an
**`**Enforced by.**`** block naming what checks it — or saying plainly that nothing does, so an
unenforced decision is visible as one rather than assumed to have a gate somewhere.
[`_template.md`](_template.md) owns that contract and states what belongs in each block. Both blocks
being *present* is checked (`test_every_record_names_what_enforces_it`, `tests/test_docs.py`);
whether the imperative is any use is a writing standard held at review, and nothing mechanises it.

Writing a new one: copy [`_template.md`](_template.md), take the next number, and add a row to both
tables below — `test_the_adr_index_and_the_adr_tree_hold_the_same_files` reads both tables and fails
on a record either one omits. Whether a thing is a decision, a term or a rule is settled in
[`docs/agents/domain.md`](../agents/domain.md).

## By area — which records govern what you are about to edit

Numbers only; the table below carries the links.

| area | ADRs |
| --- | --- |
| `src/seqforge/cli/` | 0013 |
| `src/seqforge/compose/` | 0004, 0005, 0011, 0012, 0015 |
| `src/seqforge/fingerprint/` | 0001 |
| `src/seqforge/harvest/` | 0008, 0009 |
| `src/seqforge/io/` | 0001, 0007, 0015 |
| `src/seqforge/kb/` | 0011, 0012 |
| `src/seqforge/manifest/` | 0003, 0004, 0005, 0012 |
| `src/seqforge/models/` | 0004, 0006, 0007, 0008, 0011, 0012, 0013, 0014 |
| `src/seqforge/probe/` | 0001 |
| `src/seqforge/resolve/` | 0006, 0007, 0010, 0014 |
| `src/seqforge/workflows/` | 0015 |
| `evals/` and `src/seqforge/evals/` | 0016 |
| `tests/`, and choosing which of them to run | 0002 |
| every Python file in the tree — what type-checks it, and what your editor shows | 0017 |
| the compiler as a whole — what it is *for* | 0003 |

## By number

| # | Title | The decision | Governs |
| --- | --- | --- | --- |
| [0001](0001-head-and-wholefile.md) | A probe joins a head to a whole file; there is no read-source seam | `build_observation(head, file)`; four sources keep four naming authorities and share a type, not an adapter | `probe/`, `fingerprint/load.py`, `io/remote.py`, `io/sra.py`, `models/observation.py` |
| [0002](0002-no-test-impact-analysis.md) | No test-impact analysis; the ladder is a rule, not a tool | Two markers, two narrowing tasks and a written ladder instead of a coverage-graph selector that cannot see a data edit | `tests/`, and the pixi test tasks |
| [0003](0003-manifest-is-the-missing-interface.md) | The manifest is the interface SRAgent and scRecounter never had | One compiler around a decided manifest: decide and refuse, never grid-search | the compiler as a whole; `manifest/`, `resolve/` |
| [0004](0004-two-artifacts-not-one.md) | Two artifacts: the immutable dataset and the plural recipe | `manifest.yaml` is what the data IS, `processing.yaml` what to do with it; a change of intent never moves `dataset_hash` | `models/dataset.py`, `models/processing.py`, `manifest/`, `compose/` |
| [0005](0005-run-id-is-the-pairing.md) | `run_id` hashes the pairing, and is recorded at compile time | The compiled run is identified by all four components, stored in the output and inside neither input | `manifest/hash.py`, `compose/`, `workspace.py` |
| [0006](0006-one-judgement-one-envelope.md) | One judgement, one envelope | `chemistry` is `LibrarySection`'s only `Evidenced` field; assay, read layout and per-file roles follow from it | `models/dataset.py`, `models/evidenced.py`, `resolve/` |
| [0007](0007-sample-attributes-are-ncbi-keys.md) | Sample attributes are an open dict over NCBI's 960 harmonized names | Ask a few, enforce all 960; a key outside them is a refusal, and the vocabulary is generated data | `models/dataset.py`, `io/attributes.py`, `resolve/records.py` |
| [0008](0008-llm-surface-carries-only-checkable-fields.md) | The LLM's output surface carries only fields code can re-check | `AssertionDraft` has no offsets and no `subject` — the subject is the document, and code owns both verification flags | `harvest/`, `models/assertion.py` |
| [0009](0009-llm-provider-is-pluggable.md) | The LLM provider is pluggable *because* span verification makes it swappable | Three providers ship, one prompt serves them all, and nothing downstream trusts any of them | `harvest/providers.py`, `harvest/extract.py` |
| [0010](0010-two-resolvers-one-blocks-one-warns.md) | Two resolvers, two refusals | The byte resolver blocks on an `observed`↔`asserted` conflict; the metadata resolver decides, warns, and prefers null over wrong | `resolve/records.py`, `resolve/scoring.py`, `resolve/escalate.py` |
| [0011](0011-closed-instructable-surface.md) | The instructable surface is closed, and split parse-vs-count | `backend.params` says how to parse and is never instructable; the recipe says what to count — disjoint key sets | `kb/schema.py`, `models/processing.py`, `compose/params.py` |
| [0012](0012-produce-every-answer-rather-than-ask.md) | Produce every answer rather than ask | `SoloQuant.features` defaults to all five: dissolve an ambiguity whose every answer one pass can afford | `models/processing.py`, `manifest/policy.py`, `compose/` |
| [0013](0013-cli-is-a-machine-interface.md) | The CLI is the API | JSON on stdout, logs on stderr, refusal as exit 3 or 4; no `--json`, no `--resume` | `cli/`, `models/blocker.py` |
| [0014](0014-no-inf-across-the-json-seam.md) | No ±inf crosses the JSON seam | A forbidden cell serializes as a tagged status — never the sentinel, never `null` | `resolve/scoring.py`, `resolve/assign.py`, `models/resolve.py` |
| [0015](0015-onlists-are-built-and-deleted.md) | Barcode whitelists are built by a rule and `temp()`-deleted | `rule onlist` materializes one on demand; the shipped packed array is the only stored copy | `workflows/map/`, `io/onlist.py`, `compose/` |
| [0016](0016-no-held-out-dataset.md) | No held-out dataset — a pre-registered prediction instead | The reservation is retired; `expected.yaml` is written from declared metadata before the run | `evals/`, `src/seqforge/evals/` |
| [0017](0017-one-type-checker-and-the-editor-runs-it.md) | One type checker, and the editor runs it | mypy is the only one, its scope is the whole repo, and Pylance's checker is off so the two cannot disagree | `pyproject.toml`, `.vscode/`, and every `.py` file in the tree |
