"""``kb`` — the executable, self-testing knowledge base.

One directory per technology under ``specs/`` (``spec.yaml`` + ``README.md``). Each spec validates
against :class:`~seqforge.kb.schema.Spec`, generates its own synthetic fixtures, and round-trips
through the probe. ``KB_VERSION`` (CalVer) is folded into dataset-level cache keys.
"""

from __future__ import annotations

from .generate import all_cdna_spec, build_pools, generate_reads
from .loader import (
    KbTree,
    build_tree,
    list_spec_ids,
    load_all_specs,
    load_spec,
    load_tree,
    runnable_spec_ids,
)
from .match import curated_forms, resolve_chemistry, resolve_chemistry_id
from .roundtrip import roundtrip_checks, run_roundtrip
from .schema import Spec

#: CalVer YYYY.M.PATCH; bump when spec semantics change. Folded into dataset candidate cache keys.
#: 2026.7.3 — 10x 3' v2/v3/v3.1 accept an OVER-LENGTH barcode read (R1 max_len null +
#: segment_length over_length_min 100) and add soloBarcodeReadLength:0; v2<->v3 and v2<->v3.1 declared
#: processing_divergent (whitelist-decided) for the over-length case length can no longer separate.
#: (over_length_min is 100, not 40, so a 60-94 bp cDNA/split-pool read is not mistaken for an
#: over-sequenced barcode read -> the rung-0-2 separability guard stays green without over-declaring.)
#: 2026.7.2 — bulk-rnaseq <-> splitseq declared processing_divergent, distinguishable_by onlist.
#: Found by the new rung-0-2 separability guard on its first run: the generic paired-end fallback
#: accepts SPLiT-seq's cdna+bc pair on geometry alone and had declared nothing.
#: 2026.7.1 — the parse/count line: soloFeatures / quantMode / outSAMtype left backend.params,
#: which now declares ONLY byte-decided parse keys. Also adds the 10x-3p-gex-v3.1 benign twin.
#: 2026.7.4 — added the bd-rhapsody-wta spec (BD Rhapsody WTA, original fixed-offset cell-label bead:
#: CB_UMI_Complex, three 97 x 9 bp CLS whitelists SHIPPED, two fixed linkers). bulk-rnaseq <->
#: bd-rhapsody-wta declared processing_divergent, distinguishable_by onlist (same rung-0-2 collision
#: SPLiT-seq has with the generic paired-end fallback).
#: 2026.7.5 — the KB became a TREE: added the abstract family node 10x-3p-gex (node_kind: family, no
#: backend, children_decided_by: [onlist]) that recognizes v2/v3/v3.1 and rejects bulk; the three 10x
#: leaves gained parent: 10x-3p-gex and dropped their divergent-sibling confusable_with cliques (the
#: shared parent now implies them); v3<->v3.1 processing_equivalent edges kept. Descent narrows to a
#: length-feasible pool before scoring, so adding the Nth 10x chemistry is one parent link, not a clique.
#: 2026.7.6 — first ANCHORED chemistry: the bd-rhapsody-wta-enhanced family + leaves -96 / -v2 for the
#: BD Rhapsody Enhanced bead (variable 0-3 bp diversity insert). New `diversity` element type + per-
#: element anchors; the family recognizes the GTGA/GACA frame by motif; leaves split on onlist (97 vs
#: 384 CLS pools, disjoint). Ships bd-rhapsody-cls{1,2,3}-384; -96 reuses the original bead's lists.
#: 2026.8.1 — soloCBmatchWLtype became a KB-OWNED parse key, declared by all 11 starsolo specs:
#: 1MM_multi_Nbase_pseudocounts for the 7 CB_UMI_Simple (10x) entries — CellRanger >=3's own barcode
#: correction, without which our counts are not comparable to a published CellRanger matrix — and 1MM
#: for the 4 CB_UMI_Complex ones (bd-rhapsody-wta, its two Enhanced leaves, splitseq). It was a
#: soloType branch hardcoded in starsolo.smk, which can express two answers; three are already needed,
#: because a planned Parse Evercode entry is CB_UMI_Complex too and takes EditDist_2. Legality is
#: chemistry-dependent (Complex rejects every 1MM_multi* mode including STAR's global default, Simple
#: rejects EditDist_2 — measured against the 2.7.11b binary), so the (soloType, value) pair is gated at
#: compose and an illegal one is a named refusal rather than a FATAL on a compute node.
#: Every dataset gets a new run_id: the KB version is one of its four inputs. The reprocessing that
#: costs is accepted — the alternative is a corpus whose barcode correction nobody can state.
#: 2026.8.2 — `identity.descriptive_aliases` (#266), and `bulk-rnaseq`'s four format-describing
#: aliases moved into it. The KB's own vocabulary changed, so this re-keys even though no read layout
#: did: ten strings in that vocabulary resolve to a different node than they did
#: (`_MOVED_BY_266`, `tests/test_kb.py`), and a `run_id` that did not move would be claiming a
#: chemistry decision the current KB no longer makes.
#: 2026.8.3 — the generic bulk entry is `bulk-rnaseq`, version `illumina`, and its display name no
#: longer says "paired-end". All three used to carry a `-pe`, and a `-pe` in an id is a CLAIM — that
#: this chemistry has one sequencing configuration and it has two mates. The entry is about to stop
#: making it: a single-end read set lands next, and an id contradicting what the entry declares is the
#: class of defect this tree deletes rather than annotates. Renamed AHEAD of the read set so the id is
#: never wrong, not even for the one release in between.
#: This bump costs strictly more than the ones above it, and the difference is the reason it is done
#: now. A KB version re-keys `run_id` alone, so a dataset RECOMPILES — the standing price of a
#: spec-semantics change, and it is paid here whatever else moves. But the chemistry sits in
#: `library`, and `dataset_content_hash` is taken over `library` + `experiment`, so this also moves
#: `dataset_hash`: a stored bulk manifest is not recompiled under the new id, it is REGENERATED from
#: the bytes under a new dataset hash, and every processing manifest pinned to the old one refuses
#: until it is re-pinned. The population of stored bulk manifests is smaller today than it will ever
#: be again, which is the whole argument for paying that now rather than at the next opportunity.
#: Nothing else moves. Exactly one live line reads the id — `report/collect.py`'s display map, which
#: already rendered this chemistry as "bulk RNA-seq", so the suffix was never user-facing. The reads,
#: elements, signature, backend and all five confusable edges say what 2026.8.2 said; the only other
#: files that changed are the two specs naming the edge back (`splitseq`, `bd-rhapsody-wta`), and they
#: changed by the id alone.
#: 2026.8.4 — `read_count` leaves the signature vocabulary: the model, the union member, the
#: evaluator branch and one `requires` line from each of the 16 shipped specs. A test in a CLOSED
#: vocabulary that returns `abstain / 0.0 / "not a per-cell test"` on every input is a knob that
#: cannot fail, and it was worse than idle — `build_tech_evaluation` buckets a `requires` test by its
#: `read`, and `read_count` has none, so it was dropped before it was even evaluated. Sitting in
#: `requires` it READ as the gate demanding two files, and #234 spent a whole measurement finding out
#: that the demand came from the READ LIST instead. Read sets (ADR-0029) make it doubly dead: a set's
#: cardinality IS its length, so even a working version would restate the declaration beside it.
#: Deleting it also retires a name collision this codebase should not carry: `read_count` meant role
#: count in the KB and SPOT count in ENA run metadata (`io/remote.py`), and now means only the second.
#: `bulk-rnaseq` and `10x-multiome-atac` are left with an EMPTY `requires`, and both are honest:
#: bulk is the fallback and gated nothing before either, and ATAC's "three reads" was never a gate
#: but the three reads it declares, against a total and injective role assignment.
#: The evaluator's fall-through goes with it. `evaluate` now takes the `Test` union rather than
#: `object` and ends in `typing.assert_never`, so the next word added to the DSL is a type error at
#: the dispatch that forgot it rather than a silent abstention — `compose/core.py`'s `_read_files_in`
#: is the same shape for the same reason.
#: The bump costs a `run_id` and nothing else, and that was MEASURED rather than asserted, because
#: the one thing reading cannot settle is whether an abstaining `requires` test feeds score
#: normalization. It does not: `requires` is walked for FAIL alone, and `total_w` sums `supports`
#: weights only. Re-inserting a read-less `requires` entry into every spec leaves all 256
#: (spec x data) verdicts — score, rank, matrix, assignment, rung — byte-identical, and the suite is
#: green with no expectation edited. That gate is the whole of why this shipped as its own change.
#: 2026.8.5 — `Spec.read_sets`: a spec declares a MAXIMAL read set (`reads`, implicitly named `full`)
#: and may name SUBSETS of it, and `bulk-rnaseq` declares `se: [R1]`. A single-end bulk RNA-seq FASTQ
#: resolved to `Blocker(UNSUPPORTED_TECHNOLOGY)` at exit 3 — not for failing a gate, since bulk's
#: `requires` is empty, but because a role assignment is injective AND total, so declaring two reads
#: demanded two files before any evidence was read. Single-end bulk RNA-seq is not exotic, and
#: SMART-seq3's published Methods name three configurations for one protocol.
#: The keys are a CLOSED vocabulary (a `Literal`, so `single_end:` fails at load where every other DSL
#: typo fails) and each value is a subset of ids `reads` already declares — never a re-declaration,
#: which is the whole of why the shape is cheap: R1's coordinates exist once, so the two configurations
#: of one chemistry cannot drift apart, and there is no second entry to keep in sync. A `requires` test
#: may address only reads present in EVERY set, because a gate a set cannot reach silently stops
#: gating; the rule has no instance in the shipped KB and so is held by a negative test that builds a
#: violating spec (`tests/test_kb.py`).
#: The read-set loop lives INSIDE `build_tech_evaluation`, so one spec still yields one Candidate and no
#: ranking rule needed a "a spec does not tie with itself" clause. `length_feasible` became
#: feasible-iff-ANY-set: it claims to be a proven necessary condition for a valid score, and the
#: engine's `pool = [...] or runnable` fallback would have hidden the falsification. The winning set
#: lands on the Candidate — in the resolve artifacts, where "how this was decided" lives — and NOT on
#: the manifest, whose read layout already lists exactly that set's reads.
#: The bump costs a `run_id` and nothing else. `dataset_hash` does not move: a paired-end deposit still
#: selects the maximal set at the score it always had (1.01 on the synthetic pair — the subset would
#: pay `λ/|R|` = 0.25 for the mate it declined to explain), so no stored manifest is regenerated. The
#: round-trip is NOT extended per read set, deliberately: it is per READ, from the same seed, so a
#: subset would re-run a strict subset of the same checks. Recognition was the unproven thing, and it
#: is what the new resolve cases assert.
#: 2026.8.6 — `smartseq3` declares `se: [R1]`, the second sequencing configuration its own peer-reviewed
#: Methods publish ("75-bp single end, 50-bp single end or 150-bp paired end"). 2026.8.5 built the
#: mechanism and `bulk-rnaseq` was the only entry that could use it: the plate half could not follow,
#: because `map/star-umi`'s mate-role helper RAISED rather than render an extraction with no mate.
#: ADR-0035 removes that — the tag operation is entirely WITHIN the tagged read (find the anchor, cut
#: the UMI, trim `geometry.span`), so the single-end form is the base case and the mate is an addition
#: that inherits the `UB`. This spec edit and the wider extractor are one change and not two: declaring
#: the read set alone unlocks a `umi_tagged` placement that then dies at DAG construction, which turns
#: a recoverable refusal into a failure past handover.
#: R1 and not R2, structurally rather than conventionally: R1 is the TAGGED read (`tso_tag` + `umi` +
#: `tso_ggg` + `cdna_r1`), so it is the one that survives alone, while R2 is plain cDNA and an `se` set
#: over it would declare a configuration this entry could not recognize. The set is a SUBSET of ids
#: `reads` already declares, so R1's coordinates still exist exactly once and the two configurations
#: cannot drift. The entry's one `requires` gate addresses R1, a read present in EVERY set, so
#: ADR-0029's universality rule holds and the anchored motif still gates the single-end configuration —
#: this is the rule's first shipped instance, and it is satisfied rather than exercised.
#: The bump costs a `run_id` and nothing else, and both halves of that are MEASURED rather than
#: expected, because a plate has neither thing that makes the bulk case safe: no onlist, and on a
#: one-file deposit no orphan to charge. Method, figures and the three depths they were taken at:
#: `docs/research/smartseq3-single-end-configuration.md` (2026-08-05).
#: On a PAIRED deposit the maximal set still wins, at the score this entry always had, by exactly the
#: `λ/|R|` the subset pays for the mate it declined to explain — so the same candidate is selected at
#: the same value, `dataset_hash` does not move, and no stored plate manifest is REGENERATED; every
#: dataset simply recompiles under a new `run_id`, the standing price of a spec-semantics change.
#: On a SINGLE-END deposit the contest with the generic fallback is a NEAR tie whose exact margin is
#: DEPTH-DEPENDENT — an exact tie on the truncated slice a fixture scores, +0.000999 to the plate on
#: every read — so quote no margin without its depth, and read only what survives both: inside
#: `_THETA` (0.02) either way. That is the DESIGNED outcome, not a defect: it routes to the declared
#: `smartseq3` <-> `bulk-rnaseq` edge (`processing_divergent`, `distinguishable_by: [metadata]`), i.e.
#: a Question at exit 4, which is recoverable. The signature was NOT tuned to win it outright — #257
#: measured every additional R1 support on this entry as a strict liability. Both directions ship as
#: gates in `tests/test_kb.py`, and the single-end one asserts that BULK DOES NOT WIN rather than that
#: the plate does — the asymmetry is what makes it hold at both depths, where an assertion demanding an
#: outright win would pass on a deposit and fail on the fixture that scores it.
#: 2026.8.7 — `read_through` joins the DSL, and `smartseq3` declares the first one: the Tn5 mosaic
#: end `CTGTCTCTTATACACATCT`, which nothing clipped (#356). It is the only entry declaring a
#: read-through at THIS release; the three BD Rhapsody leaves and both 10x 5' entries join it at
#: 2026.8.8, so read this as where the key started and not as where it stayed. An unclipped library
#: of this chemistry loses about a third of its reads to STAR's length-relative filters — a
#: DENOMINATOR rather than a mapping failure, since a clipped base leaves the length those filters
#: are taken over and a soft-clipped one does not. Measurements and method:
#: `docs/research/smartseq3-tn5-read-through.md`.
#: The value is TERMINAL, not a span: past the mosaic end everything is adapter, index and flowcell
#: primer, so the whole tail goes — which is what an aligner's clip already does, and why this is
#: chemistry in the entry rather than configuration in a recipe. ADR-0048 records that call, which
#: discussion #354 had reached the other way. It is the same sequence `excludes` still refuses, and
#: deliberately so: recognizing this chemistry and processing it are different questions, and the
#: abundance that disqualifies the motif from the first (#230: 6.5-79.5% of its own R1) is what makes
#: it necessary to the second.
#: Declared ONCE and never per read: the entry owes the sequence and each pipeline works out its own
#: flag, the same division that keeps the barcode geometry from being written twice. A spec declaring
#: it on a pipeline that cannot clip is refused at compose, where what each pipeline derives is known.
#: The bump costs a `run_id` and nothing else, and this one is structural rather than measured:
#: `read_through` is invisible to scoring, so no candidate moves, and it is absent from the manifest,
#: so `dataset_hash` cannot move either. There is no observed counterpart to cross-derive against —
#: the bytes cannot say what adapter a fragment ran into — which is exactly why it is declared
#: knowledge and not a probe signal. What it does cost is real and was accepted: a dataset already
#: compiled gets a new pipeline directory and its CRAMs are not reused, because the reads genuinely
#: are processed differently now. That is why it lands before large plates are mapped rather than
#: after.
#: 2026.8.8 — the read preprocessing STAR runs before it aligns is the CHEMISTRY's to name (#355).
#: `clipAdapterType` becomes a parse key REQUIRED of all eleven starsolo entries, `clip5pAdapterSeq`
#: an optional one beside it that SPLiT-seq alone declares, and five more entries declare a
#: `read_through`. It was a literal in `starsolo.smk` — `CellRanger4`, for all eleven — filed there
#: because it was chosen for CellRanger parity, which is a reason to pick a VALUE and never evidence
#: about who OWNS the key; what moved it here is that its right value moves from one chemistry to the
#: next, the test `Backend`'s docstring in `kb/schema.py` states. Required rather than
#: optional-with-a-default, deliberately — whichever group stayed silent would be defined by silence
#: and a new spec would join it by accident. The per-vendor primary-source review it rests on:
#: `docs/research/starsolo-read-preprocessing-per-family.md`.
#: **SIX ENTRIES' OUTPUT CHANGES.** `10x-5p-gex-v2` and `-v3` take `Hamming` plus
#: `read_through: CCCATATAAGAAA`, the 5' TSO's reverse complement. Dropping `CellRanger4` restores
#: parity — Cell Ranger hard-disables both its trimmers on a 5' kit — and clipping the read-through
#: then EXCEEDS the vendor, a departure taken on a measurement rather than an argument: the anchor is
#: in every 5' library measured and in no 3' control, at rates spanning two orders of magnitude
#: BETWEEN LIBRARIES of one kit — which is why BOTH entries declare it and not only the one whose
#: library carried it most. Rates and method: `docs/research/10x-5p-tso-read-through.md`. The three
#: `bd-rhapsody-wta*` entries take `Hamming` plus BD's own fixed 38-base poly-A run, the literal BD's
#: command line passes, in place of a 10x TSO their WTA protocol never used; BD leaves the clip type
#: unset entirely, so `CellRanger4` would FATAL on their own invocation. `splitseq` KEEPS
#: `CellRanger4` and fixes the sequence: its own 30 nt TSO differs from 10x's at two of thirty
#: positions, so the clip was already firing here with the wrong string on a 66 bp cDNA read, and the
#: override REPLACES the hardcoded one.
#: **FIVE ARE UNTOUCHED, and that is the acceptance criterion.** `10x-3p-gex-v2`, `-v3`, `-v3.1`,
#: `10x-gemx-3p-v4` and `10x-multiome-gex` declare `CellRanger4` and emit a BYTE-IDENTICAL STAR
#: command line — diffed before and after by three separate steps rather than inferred from the value
#: — so the counts a published CellRanger matrix is comparable to do not move. Cell Ranger builds its
#: two fixed adapters under ONE predicate, the kit's endedness, and all five 3' chemistries take that
#: branch, so one answer is correct for all of them.
#: An illegal pairing is refused at spec LOAD by one rule — the end a declared clip sits at must be an
#: end its declared trimmer takes — and not by a list of the two pairs that are illegal. STAR refuses
#: the pair at parameter initialization, BEFORE the genome loads, so what this replaces is every
#: sample of a deposit failing after its queue wait over a flag nobody typed.
#: **THE ELEVEN RE-KEY AND THE REST OF THE CORPUS DOES NOT.** `run_id` folds a content hash of the
#: processing half of the spec that decided a dataset (ADR-0037), never this global string, so what a
#: release re-keys is what it actually moved. All eleven starsolo entries here gain a
#: `clipAdapterType`, which is a backend param and so is hashed, so all eleven get a new pipeline
#: directory and no reuse of the BAMs in the old one — six because the STAR command line genuinely
#: changed and five because the declaration arrived in the spec even though the command line it
#: renders did not move. A Smart-seq3 plate, a bulk deposit, or any other chemistry this release did
#: not touch keeps the directory it already has.
#: `dataset_hash` does not move and no stored manifest is REGENERATED. Backend params live in the KB
#: and never in a manifest, `read_through` is absent from it too, and nothing scoring reads — reads,
#: elements, signature, confusable edges — changed, so every candidate is still selected at the value
#: it always had and a stored manifest recompiles rather than being rebuilt from the bytes.
KB_VERSION = "2026.8.8"

__all__ = [
    "KB_VERSION",
    "Spec",
    "KbTree",
    "load_spec",
    "load_all_specs",
    "load_tree",
    "build_tree",
    "list_spec_ids",
    "runnable_spec_ids",
    "resolve_chemistry",
    "resolve_chemistry_id",
    "curated_forms",
    "all_cdna_spec",
    "generate_reads",
    "build_pools",
    "run_roundtrip",
    "roundtrip_checks",
]
