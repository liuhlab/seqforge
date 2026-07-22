"""``resolve`` — the scoring engine: bytes + KB (+ optional hypothesis) -> a ranked, escalated verdict.

Deterministic and LLM-free. Signature-test evaluators score a JSON-safe evidence matrix
``M[role][file]``; a cardinality-normalized joint role-assignment picks the best injective
files->roles map per technology; escalation turns the ranked candidates into exactly one of
``Decision`` / ``Conflict`` / ``Question`` / ``Blocker`` with rung provenance. Every artifact is
content-addressed under ``.seqforge/``. The only interpretive input is a span-verified
``hypothesis`` that steers control flow — it never enters the matrix (§3.4).
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
RESOLVE_VERSION = "2026.7.12"

from .cache import Cache, dataset_id  # noqa: E402
from .engine import (  # noqa: E402
    Hypothesis,
    MultiRunOutput,
    ResolveOutput,
    RunResolution,
    exit_code_for,
    resolve_dataset,
    resolve_runs,
    role_of_sha_for,
)
from .group import group_runs, run_key  # noqa: E402
from .scoring import Cell, TechEvaluation, build_tech_evaluation  # noqa: E402
from .window import WindowProbe  # noqa: E402

__all__ = [
    "RESOLVE_VERSION",
    "resolve_dataset",
    "resolve_runs",
    "ResolveOutput",
    "MultiRunOutput",
    "RunResolution",
    "role_of_sha_for",
    "group_runs",
    "run_key",
    "Hypothesis",
    "exit_code_for",
    "build_tech_evaluation",
    "TechEvaluation",
    "Cell",
    "WindowProbe",
    "Cache",
    "dataset_id",
]
