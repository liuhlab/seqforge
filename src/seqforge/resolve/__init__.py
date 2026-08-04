"""``resolve`` — the scoring engine: bytes + KB (+ optional hypothesis) -> a ranked, escalated verdict.

Deterministic and LLM-free. Signature-test evaluators score a JSON-safe evidence matrix
``M[role][file]``; a cardinality-normalized joint role-assignment picks the best injective
files->roles map per technology; escalation turns the ranked candidates into exactly one of
``Decision`` / ``Conflict`` / ``Question`` / ``Blocker`` with rung provenance. Every artifact is
content-addressed under ``.seqforge/``. The only interpretive input is a span-verified
``hypothesis`` that steers control flow — it never enters the matrix.
"""

from __future__ import annotations

#: CalVer YYYY.M.PATCH; bumped when scoring/assignment/escalation semantics change. Folded into the
#: dataset cache key so a resolver change invalidates stale candidates.
#: 2026.7.1 — `resolve_runs`: files are grouped into runs and each run is assigned on its own
#: bytes. A dataset resolved as one library dropped every file but one pair per role.
#: 2026.7.2 — over-length onlist admission: a barcode read over-sequenced into the length dead zone
#: (canonical < mode < over_length_min) is admitted when its barcode prefix hits the whitelist, so a
#: previously-forbidden over-sequenced read now resolves to its chemistry (#7).
#: 2026.7.3 — over-length admission uses a FLOOR-ANCHORED bar, not the support `min`: admission asks
#: "barcode vs cDNA" (chance ≈ whitelist floor), not "confident barcode" (0.6). A real over-sequenced
#: barcode read with ordinary sequencing error hit below 0.6 on exact match and fell to bulk (#7,
#: GSE126954 SRX5411291); the floor-anchored bar admits it while still rejecting a same-length cDNA.
#: 2026.7.4 — multi-lane surplus absorption: a run sequenced across N lanes holds N files per role, but
#: the injective assignment fills each role once; the surplus same-length lane files are now absorbed
#: into their role (was NO_VALID_ROLE_ASSIGNMENT), so a multi-lane 10x dataset resolves (GSE208154).
#: 2026.7.5 — surplus absorption matches by READ DESIGNATION (R1/R2/…) + length, not de-laned filename.
#: One accession sequenced across several flowcells carries a different flowcell id per file, so the
#: lanes of one read de-laned to different names and the cross-flowcell surplus stayed unassigned;
#: matching on the designation the sequencer wrote fuses them (GSE208154 is 2 flowcells x 8 lanes x
#: {R1,R2,I1} per run, which 2026.7.4's de-lane equality could not absorb across the flowcell boundary).
#: 2026.7.6 — role assignment optimizes (coverage, score) lexicographically, not score alone: a file
#: eligible for exactly one role claims it before a multi-role file can. GSE208154's real cDNA reads
#: have low-diversity 5′ ends, so a 28 bp barcode read out-scored the 91 bp cDNA read for the cDNA role;
#: score-max then took a barcode file for cDNA and orphaned every cDNA-length file (absorption could not
#: recover — the cDNA rep was itself a barcode read). The 91 bp reads are forbidden for the barcode role
#: (dead zone), so cDNA is their only home; coverage now seats them there. No-op for one-file-per-role
#: runs (injectivity already forces the map), so the other 12 worm datasets are unaffected.
#: 2026.7.7 — hierarchical descent: resolve_dataset scores a length-FEASIBLE pool (drawn from runnable
#: specs via the scorer's own read-length gate) instead of a flat loop over the whole KB; escalate still
#: receives the full KB. Provably winner-invariant — a length-infeasible spec would have scored
#: forbidden — so the winner equals a flat full scan; this only narrows which specs are scored as the KB
#: grows, and reads sibling confusability off the tree instead of hand-declared cliques.
#: 2026.7.8 — family-level chemistry authority: a WITHIN-family asserted-vs-observed geometry difference
#: (asserted v2, observed v3 — both 10x-3p-gex leaves) is no longer a blocking conflict. A paper names
#: the assay family reliably and the leaf vaguely; the bytes decide the leaf, so the disagreement is
#: recorded as a `resolved` conflict (auditable, non-blocking) instead of exit 4. A CROSS-family
#: difference still blocks. Auto-resolves GSE229022 ("10x 3' v2/v3" in prose, byte-provably v3).
#: 2026.7.9 — cross-family honesty made symmetric: a BULK chemistry asserted but a barcoded single-cell
#: library observed now surfaces a conflict (exit 4), the mirror of single-cell-asserted-but-bulk-observed
#: (which already did). Both directions of a wrong data-vs-paper pairing are now caught, not just one.
#: 2026.7.10 — BD Rhapsody Enhanced beads: anchored barcode elements are located by their adjacent
#: anchor sequence before the onlist window is read, so an Enhanced (diversity-spacer) library scores its
#: CLS whitelists at the right offset instead of missing them at a fixed one (#53).
#: 2026.7.11 — barcode-role seating + barcode-absent refusal (F1): the barcode role may only seat on a
#: read that clears the onlist bar when some read does (no equal-length swap onto a cDNA mate), and a
#: barcoded winner with NO whitelist-hitting read now refuses (BARCODE_READ_ABSENT) instead of composing
#: a pipeline STARsolo would run to an empty matrix.
#: 2026.7.12 — barcode-absent refusal keys on ALL valid candidates, not just the top: an over-length
#: v2/v3 tie where v2 edges v3 on score (top=v2, 737K misses) while v3's 3M list hits must NOT refuse —
#: the data is barcoded and resolves to v3. Only a dataset where no barcoded leaf hits is barcode-absent
#: (PRJNA658829 SRR12575567 was false-blocked before this).
#: 2026.7.13 — persist the evidence matrix as a cache sidecar (`cache/matrices/<ds_id>.json`) beside the
#: candidates, for the `seqforge report` glance layer. Pure cache-write addition: no scoring/escalation
#: semantics change, so candidate VALUES are byte-identical — but the write is a resolver behaviour, so
#: the version bump re-keys `ds_id` and datasets re-resolve once (cheap: N=2000, no LLM) to populate it.
#: `dataset_hash` (the manifest's identity) does not fold RESOLVE_VERSION and is unaffected.
#: 2026.7.15 — `has_segment kind: constant` becomes a floor on the SHARE OF READS carrying the window's
#: modal consensus, replacing a mean per-cycle purity that could not tell "every read carries this
#: linker" from "most do and the rest of the head is junk" (#149). This one MUST re-key: the defect it
#: fixes is a cached verdict. A real SPLiT-seq dataset already resolved to `bulk-rnaseq-pe` at exit 0
#: would otherwise keep serving that candidate straight out of the cache, and the fix would land
#: green while changing nothing anyone could observe.
#: 2026.7.16 — the onlist hit rate's DENOMINATOR counts only reads that could have hit: a window
#: holding a non-ACGT base is unpackable, so it leaves `n_tested` instead of diluting the rate (#177).
#: Both the fixed-offset and the anchored path now share that policy. This MUST re-key for the same
#: reason 2026.7.15 did — the defect it fixes is a cached REFUSAL. `evals/benchmark/GSE305031` (a real
#: GEM-X 3' v4 worm library with a dark cycle at R1 cycle 2) is already sitting in caches as
#: BARCODE_READ_ABSENT, and would keep being served straight out of one while the fix landed green.
#: 2026.7.17 — an asserted chemistry must NAME one: `resolve_chemistry` matches a KB node by
#: one-directional entailment (a curated alias inside the value, never the value inside an alias), and
#: a family term that narrows to the observed leaf is agreement rather than a conflict (ADR-0020).
#: This MUST re-key, and for the reason 2026.7.15 and 2026.7.16 did — the defect it fixes is a cached
#: REFUSAL. Every transcriptomic run in SRA carries `library_strategy: RNA-Seq`, which the old matcher
#: read as `bulk-rnaseq-pe`, so a byte-provably single-cell dataset is already sitting in caches as
#: `conflict-bulk-asserted-single-cell-observed` at exit 4. Without the bump those datasets would keep
#: being served that refusal out of the cache while this landed green and changed nothing anyone could
#: observe. `evals` run with `use_cache=False`, so the benchmark cannot report this either way.
#: 2026.7.18 — `PRETRIMMED_VARIABLE_LENGTH` is decided on a FLOOR under the share of a fixed-cycle
#: read's reads that sit at its modal length, not on "every read agrees" (#190). The gate was
#: `n_distinct == 1`, so one read a single base short in a 2 000-read head refused the dataset at
#: exit 3 with no appeal — the same failure, and the same cure, as 2026.7.15 one layer over: a
#: statistic that cannot tell "every read is this length" from "most are and the rest of the head is
#: ragged". The bar is that entry's majority, for that entry's reason.
#: This MUST re-key, and for the reason 2026.7.15/.16/.17 did — the defect it fixes is a cached
#: REFUSAL. A dataset with a ragged tail is already sitting in caches as PRETRIMMED_VARIABLE_LENGTH at
#: exit 3, and would keep being served that refusal while this landed green. The accompanying
#: PROBE_VERSION bump (2026.8.1, which adds the share this reads) moves `dataset_id` as well, but
#: that is a probe fact: the resolver's own stamp is what states that the resolver's verdict is stale,
#: and a reader of this log must not have to cross-reference another module's to learn it.
#: 2026.8.4 — `motif_present` adopts the coverage policy `2026.7.16` gave the two onlist paths: an
#: uncalled base is not a substitution, so it no longer eats the `max_mismatch` budget, and a read
#: never called where the motif was looked for leaves `n_tested` instead of diluting the rate (#255).
#: Only the offsets a motif CONSTRAINS can be lost — a cycle under an `N` was never evidence — and
#: what a loss costs depends on what the search declares: `read_start`/`read_end`/a closed `window`
#: name where the motif is, so an uncalled base at any of their constrained offsets costs the whole
#: read; `anywhere`, and a `window` left open at the end, declare nothing and cost only the positions
#: they reach. This MUST re-key for the reason 2026.7.15/.16/.17/.18 did — the
#: defect it fixes is a cached REFUSAL. The gate is a `requires` at a majority threshold on all three
#: shipped BD Rhapsody Enhanced specs, so one dark cycle in barcode-read cycles 8-29 invalidated the
#: whole family at once, and such a dataset would keep being served that verdict out of the cache
#: while this landed green.
#: 2026.8.5 — a run is LANE-BLIND: `run_key` strips a trailing `_L\d{3}` once the mate token is off,
#: so the four lanes of one library are one run (#263, ADR-0027). This MUST re-key, and for the
#: reason 2026.7.15/.16/.17 and 2026.8.4 did — what sits in caches is a wrong GROUPING, and with no archive record
#: a run IS the sample identity, so a four-lane library is cached as four samples at a quarter depth
#: each: self-consistent, `validate` clean, exit 0. Nothing refuses it and nothing would notice.
#: `dataset_hash` moves with it for record-less multi-lane data, which is the point — a pin to the
#: four-sample manifest refuses rather than resolving to a different dataset than it was written for.
#: 2026.8.6 — chemistry matching ranks by specificity, not by an alias's token count (#266,
#: ADR-0028). ADR-0020 states the obligation this discharges: the tie-break is part of the verdict,
#: so a change to it is a change to a cached one. The stale entries are the dangerous direction —
#: a dataset whose metadata said "SPLiT-seq paired-end RNA-seq" is cached as `bulk-rnaseq-pe` at
#: exit 0, a confident wrong answer that would keep being served while this landed green.
RESOLVE_VERSION = "2026.8.6"

from .cache import Cache, dataset_id  # noqa: E402
from .engine import (  # noqa: E402
    DatasetResolution,
    Hypothesis,
    MultiRunOutput,
    ResolveOutput,
    RunResolution,
    chemistry_hypothesis,
    exit_code_for,
    reduce_dataset,
    resolve_dataset,
    resolve_runs,
    role_of_sha_for,
)
from .group import group_runs, lane_of, run_key  # noqa: E402
from .scoring import Cell, TechEvaluation, build_tech_evaluation  # noqa: E402
from .window import WindowProbe  # noqa: E402

__all__ = [
    "RESOLVE_VERSION",
    "resolve_dataset",
    "resolve_runs",
    "ResolveOutput",
    "MultiRunOutput",
    "RunResolution",
    "DatasetResolution",
    "reduce_dataset",
    "role_of_sha_for",
    "group_runs",
    "run_key",
    "lane_of",
    "Hypothesis",
    "chemistry_hypothesis",
    "exit_code_for",
    "build_tech_evaluation",
    "TechEvaluation",
    "Cell",
    "WindowProbe",
    "Cache",
    "dataset_id",
]
