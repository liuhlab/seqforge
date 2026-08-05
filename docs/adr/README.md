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
| `src/seqforge/cli/` | 0013, 0018, 0031, 0033, 0034, 0035 |
| `src/seqforge/compose/` | 0004, 0005, 0011, 0012, 0015, 0022, 0024, 0027, 0029, 0030, 0032, 0035 |
| `src/seqforge/fingerprint/` | 0001 |
| `src/seqforge/harvest/` | 0008, 0009, 0020, 0021, 0028, 0030 |
| `src/seqforge/io/` | 0001, 0007, 0015, 0018, 0033 |
| `src/seqforge/kb/` | 0011, 0012, 0020, 0022, 0028, 0029, 0032, 0035 |
| `src/seqforge/manifest/` | 0003, 0004, 0005, 0012, 0030, 0033 |
| `src/seqforge/models/` | 0004, 0006, 0007, 0008, 0011, 0012, 0013, 0014, 0023, 0030, 0033, 0034 |
| `src/seqforge/probe/` | 0001 |
| `src/seqforge/report/` | 0024, 0025, 0026 |
| `src/seqforge/resolve/` | 0006, 0007, 0010, 0014, 0020, 0021, 0027, 0028, 0029, 0030, 0032, 0033, 0034 |
| `src/seqforge/workflows/` | 0015, 0022, 0023, 0025, 0026, 0027, 0029, 0035 |
| `pipeline.py`, `workspace.py`, `e2e.py` — the compiled pipeline's layout | 0005, 0024, 0032 |
| `recordset.py` — one loader for both record-set dialects, and the draft | 0034 |
| `evals/` and `src/seqforge/evals/` | 0016, 0018, 0034 |
| `tests/`, and choosing which of them to run | 0002 |
| every Python file in the tree — what type-checks it, and what your editor shows | 0017 |
| the compiler as a whole — what it is *for* | 0003 |

## By number

| # | Title | The decision | Governs |
| --- | --- | --- | --- |
| [0001](0001-head-and-wholefile.md) | A probe joins a head to a whole file; there is no read-source seam | `build_observation(head, file)`; four sources keep four naming authorities and share a type, not an adapter | `probe/`, `fingerprint/load.py`, `io/remote.py`, `io/sra.py`, `models/observation.py` |
| [0002](0002-no-test-impact-analysis.md) | No test-impact analysis; the ladder is a rule, not a tool | Two markers, two narrowing tasks and a written ladder instead of a coverage-graph selector that cannot see a data edit | `tests/`, the pixi test tasks, and what CI may run over them |
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
| [0018](0018-a-red-benchmark-case-is-published-anyway.md) | A benchmark case a real dataset makes red is published anyway | Never withhold a package to keep a rate green; a package the corpus does not hold reports **absent**, not a skip | `evals/`, `src/seqforge/evals/`, `io/benchmark.py`, `cli/io.py` |
| [0020](0020-a-family-term-narrows-it-does-not-conflict.md) | A family term narrows; it does not conflict | One one-directional matcher answers what a chemistry string NAMES; a value naming no node asserts nothing, and an ancestor of the observed leaf is agreement | `kb/match.py`, `resolve/escalate.py`, `resolve/confuse.py`, `harvest/verify.py` |
| [0021](0021-one-deposit-is-one-source-at-every-layer.md) | One deposit is one source, at every layer | The slot a submitter typed outranks a model's reading of that same deposit, and a span byte-equal to a typed column is not prose to read | `resolve/records.py`, `harvest/normalize.py`, `harvest/verify.py`, `harvest/plan.py` |
| [0022](0022-three-owners-for-an-aligner-param.md) | An aligner param has three owners, not two | The KB says how to parse, the recipe what to count, and the module carries every flag whose value varies with nothing — as a literal | `workflows/map/`, `kb/specs/`, `compose/params.py` |
| [0023](0023-star-memory-escalates-on-retry.md) | STAR's memory escalates on retry, and a job that still does not fit fails loudly | `mem_gb` is the FIRST attempt's request, every cap STAR is handed derives from the escalated one, and exhausting the retries is a legible refusal rather than a bigger default for everyone | `workflows/memory.py`, `workflows/map/starsolo.smk`, `models/processing.py` |
| [0024](0024-one-owner-for-the-compiled-pipeline.md) | The compiled pipeline directory has one owner, and it sits above the composer | `workspace.py` names the directory and does no I/O; a top-level `pipeline.py` names the three files and answers which module ran, the config, the samples and where the outputs are — the composer writes through it and every other consumer reads through it | `pipeline.py`, `workspace.py`, `compose/`, `report/collect.py`, `e2e.py` |
| [0025](0025-the-module-that-writes-an-artifact-owns-reading-it.md) | The module that writes a QC artifact owns reading it, and a registry names who does not | A leaf metrics vocabulary, one adapter beside each writer, and a `StatsSpec` registry whose `MODULES_WITHOUT_STATS` half makes "reports nothing" a declaration rather than a silent fall-through | `workflows/metrics.py`, `workflows/stats.py`, `workflows/qc.py`, `report/` |
| [0026](0026-alerts-are-advisory-and-non-mutating.md) | An alert is advisory — the first backward edge writes nothing and changes no exit code | Post-run evidence names the decision it implicates and the value currently set; it rewrites neither artifact, raises no exit code and produces no refusal — the user decides whether to recompose | `workflows/metrics.py`, `workflows/stats.py`, `report/collect.py`, `report/panels.py` |
| [0027](0027-a-run-spans-its-lanes.md) | A run spans its lanes, and filenames group no further | `run_key` strips `_L` + exactly three digits and nothing else — never `_S<n>`, never conditioned on the directory — and the lane survives in `units.tsv` to order a run's files identically for every mate | `resolve/group.py`, `compose/core.py`, `workflows/units.py`, `workflows/map/` |
| [0028](0028-specificity-not-verbosity-ranks-a-chemistry-match.md) | Specificity, not verbosity, ranks a chemistry match | A form that only describes a run is declared as one and never outranks a chemistry's own name; tokens then entailment settle the rest, and every component reads only the two strings | `kb/match.py`, `kb/schema.py`, `kb/specs/`, `harvest/extract.py` |
| [0029](0029-a-spec-declares-read-sets-not-a-fixed-read-list.md) | A spec declares read sets, not a fixed read list | `reads` is the maximal set and `read_sets` names subsets of its ids, so one chemistry covers the paired-end and single-end configurations its protocol publishes; every set is complete, so role assignment stays injective and total | `kb/schema.py`, `kb/specs/`, `resolve/scoring.py`, `resolve/geometry.py`, `compose/core.py`, `workflows/` |
| [0030](0030-a-measurement-lives-in-provenance.md) | A measurement the dataset's identity must exclude lives in provenance | Per-file read counts keyed by sha256, on every manifest and outside every hash; the threshold over them is applied at compose under the live KB and never frozen into the write-once manifest as a verdict | `models/dataset.py`, `manifest/fill.py`, `manifest/hash.py`, `compose/` |
| [0031](0031-a-collapsed-citation-is-regenerable-only-from-the-record-set.md) | A collapsed citation is regenerable only from the record set, so harvest writes every member | Near-identical records fold onto one exemplar at plan time; a claim fans iff its quote touches no variant span, sample-scoped claims materialize per member so `_basis_for` is untouched, and every rendered member — sent or not — reaches `documents/` and `document_subjects` | `harvest/plan.py`, `harvest/normalize.py`, `cli/harvest.py`, `models/assertion.py`, `resolve/records.py` |
| [0032](0032-a-spec-declares-the-shape-of-a-deposit.md) | A spec declares the shape of a deposit, and compose acts on that declaration without the manifest recording the outcome | `identity.sample_is_cell` and `min_input_reads` are declared, never derived; the floor is applied at every compile under the live KB, over the `Sample` (min within a run, sum across them), and the starved sample leaves `config["samples"]` and `units.tsv` for a written exclusion record while staying in the manifest, so moving the threshold never moves `dataset_hash` | `kb/schema.py`, `compose/admission.py`, `compose/core.py`, `pipeline.py`, `resolve/engine.py` |
| [0033](0033-a-submitted-file-is-a-transcript-entry-not-a-checksum.md) | A submitted file is a transcript entry, and its md5 is an address we never check | `ArchiveRecord` carries name, provider md5, size and URI per file the submitter uploaded; the md5 addresses hosted bytes and is never computed against a local file, the size checks a filename-made join and never creates one, and the URI is printed only where the record set is in hand | `models/records.py`, `io/archive.py`, `io/remote.py`, `resolve/records.py`, `resolve/escalate.py`, `manifest/validate.py`, `cli/io.py` |
| [0034](0034-a-user-record-set-declares-structure-never-a-fact.md) | A user-written record set declares structure, never a fact | A `source: user` set carries `level`/`id`/`parent`/`filenames` and no attributes — which is what keeps `asserted` meaning an archive's typed slot; two levels (`run → sample`), and fusing runs the filenames separate warns rather than blocks | `recordset.py`, `cli/records.py`, `models/records.py`, `resolve/records.py`, `cli/manifest.py`, `cli/run.py`, `evals/case.py` |
| [0035](0035-the-mate-is-an-addition-to-umi-extraction.md) | The mate is an addition to UMI extraction, not half of it | The tag operation is entirely within one read, so the single-end form is the base case and the pairing is the addition: one verb with a nullable mate, the module deriving both the mate argument and `SAM SE`/`SAM PE` from `read_files_in`, and no signature tuned to win a read-set contest | `workflows/umite/extract.py`, `workflows/map/star-umi.smk`, `cli/io.py`, `kb/specs/` |
