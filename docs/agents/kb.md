# The knowledge base: `spec.yaml`, confusability, and the round-trip

Read this when you add or edit a technology entry — it is the reference behind the
`seqforge-kb-author` skill. R8 in detail: every entry is executable and self-testing, so the entry you
write is the entry CI runs. Terms (`Spec`, `Backend params`, `Onlist`, `Confusable`,
`Processing-equivalent`, `Processing-divergent`) are defined in [`CONTEXT.md`](../../CONTEXT.md).

## Layout and why the schema is Pydantic

One directory per technology under `src/seqforge/kb/specs/<tech>/`, holding two files:

- **`README.md`** — prose for the LLM and for a human: how the assay works, its aliases, its gotchas,
  the shapes it arrives in from SRA. This is the file that renders into the published site.
- **`spec.yaml`** — machine-checkable, and the only file anything decides from.

The schema is a Pydantic v2 model ([`kb/schema.py`](../../src/seqforge/kb/schema.py)) with
`extra="forbid"` on **every** model, including each signature-test leaf, so a typo'd key fails
validation exactly where the DSL is executed rather than being silently ignored. The reason for
Pydantic here is R8 — one executable validator that also self-tests — **not** R1's LLM-output clause.
A `spec.yaml` is human-authored and CI-validated; no model writes one.

The tree is hierarchical: an abstract family node (`10x-3p-gex`, which carries no backend) descends to
leaf chemistries.

## The schema decisions a field list cannot show

- **An `Element` has exactly one coherent addressing mode** — a fixed `[start, end)` XOR an `anchor`
  (a floating element) XOR `min_len`/`max_len` — enforced by a model validator. `linker` and `fixed`
  elements require a `sequence`, and an open `end: null` is allowed only for `cdna` and `gdna`.
- **The `signature` tests are a closed set, identical to the scorer's evaluators**
  ([`resolve.md`](resolve.md)). `requires` are hard AND-gates and may not use a distinct ratio, which
  is depth-dependent; `supports` are additive positive evidence, and this is where an onlist test and
  a distinct ratio belong; `excludes` are anti-gates, and any pass disqualifies. `read_count` counts
  biological and barcode **roles**, not raw files.
- **`Backend.params` is the chemistry-defining minimum only** — how to *parse* reads. CellRanger-parity
  knobs are recipe policy, not chemistry (below). The one interpolation token allowed anywhere in it is
  `{onlist:<alias>}`, and it is validated: any other `{…}` fails.
- **`decidable_by` is a derived `Spec` property, not a stored field** — the union over the
  processing-divergent confusables of the minimal sufficient mechanism. It used to be hand-typed on
  every spec, read by nothing, under a comment claiming a "CI-computed union" that no CI computed, so
  it drifted freely with nothing to notice. The derivation reproduces all five hand-typed values
  exactly, which is how you know it was only ever a comment. Two other fields died the same way; if
  you are about to add a field nothing reads, that is the pattern.
- **`Spec._cross_refs` resolves everything by name**: every test's `read` and `element`, every
  `anchor.ref_element`, and every onlist alias, against the reads and elements block. A dangling name
  is a load-time failure, not a scoring-time surprise.
- **Every element declares a `seqspec_region_type` and every read a `seqspec_read_id`.** seqspec's
  `Assay`/`Read`/`Region` decomposition is adopted, so an export is a pure derivation rather than a
  translation. The *emitter* is unbuilt.

### What moved out of the backend

CellRanger-parity knobs — `soloUMIdedup 1MM_CR`, `soloUMIfiltering MultiGeneUMI_CR`,
`clipAdapterType CellRanger4`, `outFilterScoreMin 30` — are processing **policy**, not chemistry. The
parse-versus-count key split that puts them in the recipe, and thereby makes "a user instruction
contradicts the bytes" inexpressible, is
[ADR-0011](../adr/0011-closed-instructable-surface.md).

Positions are the same story one level down: `soloCBposition` and `soloUMIposition` are **omitted from
the KB and derived from the element coordinates at compose time**, never hand-typed. Two spellings of
one geometry is one spelling too many.

## Every value is pinned to a live source, or it does not enter the KB

A chemistry fact is looked up against a live source and pinned by URL plus checksum, or it does not go
in a `spec.yaml`. Never assert one from memory. A wrong whitelist does not fail — it emits a
thin-looking matrix, which is the worst failure mode available here: a refusal is recoverable and a
plausible matrix is not. Barcode files, linker sequences, strand, and ontology terms are all the same
value under this rule. A value that is not yet pinned is an open lookup in the tracker, never a
placeholder in a spec — an unverified value parked in prose is a value nobody checks.

**Ontology terms checked against the live EBI OLS** (not memory), recorded so nobody re-opens them:
`EFO:0009922` (10x 3′ v3), `EFO:0009899` (10x 3′ v2), `EFO:0009919` (SPLiT-seq), `EFO:0008896` (bulk
RNA-Seq). v3.1 carries its own term, `EFO:0022980`, and Parse Evercode's are `EFO:0022600/1/2` —
distinct assays, and conflating them files a spec under a protocol it does not model. Each spec repeats
its own verification note beside `assay_ontology`. **Any future technology's term is looked up the same
way before use.**

### Pinned ahead of the spec that will need them

A value looked up for a technology we do not yet model has no `spec.yaml` to live beside, and putting
it back in the tracker invites the next author to look it up again or, worse, to assert it. These are
verified and shipped, so the entry that needs them starts from a pin rather than from memory.

**`3M-3pgex-may-2023`** — the GEM-X 3′ v4 whitelist, 7 372 800 × 16 bp, packed from
`scg_lib_structs/data/10X-Genomics/`. The `10x-gemx-3p-v4` spec does not exist; until it does, v3 and
v3.1 declare it as a forward confusable and rung 3 has nothing to compare against. The separation it
is declared on is measured, not assumed — against `3M-february-2018` the two share 68 254 barcodes, so
a v4 library hits the v3 list at most **0.9%**.

**10x 5′ whitelists.** v1/v2 reuse `737K-august-2016`, which is 3′ v2's list — genuinely coincident,
and the case that makes 5′-vs-3′ read-undecidable. v3 does **not**: it uses `3M-5pgex-jan-2023`
(3 686 400 × 16 bp), which shares 0.62% with `3M-february-2018` and 6.87% with `3M-3pgex-may-2023`. So
"5′ is separable only by metadata or alignment" is true of the *version pairing*, not of 5′ as such,
and an eventual `10x-5p-gex` entry should be version-qualified rather than inherit the blanket claim.

**10x 5′ `soloStrand` is NOT pinned, and must not be shipped from the evidence below.** Recorded so
the search is not repeated: scg_lib_structs has 5′ read 2 sequencing the top strand as template, which
is antisense to the mRNA — that reads as `Reverse` — and community practice agrees, with the caveat
that paired-end mapping that over-sequences the adapter read flips it to `Forward`. STAR's own manual
defines `Forward`/`Reverse` and takes **no position** on 5′. That is materially weaker than the bar
`splitseq`'s strand met (a derivation from the kit's own oligos, corroborated by the authors' code),
and strand is the value where being wrong is quietest. Deriving it from the GEM-X 5′ user guide's oligo
architecture, or running one real 5′ library both ways, is what closes it.

## Two worked entries

**`10x-3p-gex-v3`** is the fixed-offset case: R1 is 28 bp of 16 bp CB plus 12 bp UMI
(`soloType CB_UMI_Simple`), R2 is open-ended cDNA. Its `signature` shows the rung structure clearly —
`requires` are structural gates friendly to the cheap rungs (read count, 28 bp segment length, two
random segments; **no** onlist and **no** distinct ratio), `supports` add the onlist hit rate that
costs a rung-3 lookup plus depth-dependent distinct-ratio priors, and `excludes` anti-gate the
Multiome whitelist.

Its `confusable_with` block is the load-bearing part. v3.1 is `processing_equivalent` with
`distinguishable_by: [none]` — identical geometry, whitelist and params, so a tie between them is
benign and asks zero questions. Multiome and GEM-X v4 share the same 28 bp / 16+12 geometry and are
`processing_divergent`, separated only by an onlist at rung 3 — which is why a GEM-X entry is
*required* for the flagship to pass its own under-declaration check. 10x 5′ is a different problem:
its geometry and whitelist coincide with 3′, so it is read-undecidable and must be settled from
metadata or an alignment.

**`splitseq`** is the combinatorial case: the cell barcode is three round-specific 8 bp barcodes drawn
from small (~96-entry) whitelists, separated by two fixed 30 bp linkers. The positions are fixed, so
it needs no `anchor` — `soloType CB_UMI_Complex`, with the three round whitelists concatenated in
**positional** order, because rounds map to CB positions in order. Nothing may sort that list. Its
signature gates the two linkers as `requires` constant-segment tests, scores the three round barcodes
as onlist `supports`, and `excludes` a 16 bp 10x CB hit.

Two disciplines that entry demonstrates, and that a new entry should copy:

- **Scope the entry to one published protocol.** `splitseq` models the *original* published SPLiT-seq
  only (Rosenberg et al., *Science* 2018, `doi:10.1126/science.aam8999`). Parse Biosciences Evercode
  is an actively-versioned commercial descendant with different linkers and whitelists; it is deferred
  to its own future entry and never conflated. Two protocols in one spec is the failure that makes an
  entry unfalsifiable.
- **Pin the read structure to a citable source, verbatim.** SPLiT-seq's layout and both linker
  sequences are taken from
  [scg_lib_structs](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html) (CC-BY),
  not from memory.

The `anchor` path — a floating element whose frame is recovered per read — is exercised instead by
**BD Rhapsody Enhanced**, whose 0–3 bp diversity insert floats every CB block and whose motif-anchored
resolver ([`kb/anchor.py`](../../src/seqforge/kb/anchor.py)) phase-locks the `GTGA`/`GACA` linkers to
recover the frame. A read whose frame is not found contributes nothing, rather than a wrong window.

## The confusability matrix, computed and not written down

For every ordered pair of specs, CI derives three facts and then validates the declared labels against
them. There is no hand-maintained truth table.

1. **Is the pair separable at the cheap rungs?** Generate synthetic reads from A's declared layout, run
   the non-onlist subset of A's signature against B's synthetic reads, and symmetrically.
   **Under-declaration is a CI error:** not separable, and B is not in A's `confusable_with`. That is
   the guard that makes a GEM-X entry mandatory once the flagship exists. **Over-declaration is a CI
   warning:** separable, but declared confusable anyway.
2. **Do their onlists separate them?** True only if the two whitelists have a low cross-hit rate,
   computed by an actual set intersection over the packed barcode arrays — **not** by comparing
   checksums. Different hashes prove the files differ, not that the barcode sets differ, and a
   whitelist that is a superset of another has equal hashes to nothing and separates nothing.
3. **Are their backends identical?** Resolve each `backend.params`, expand every `{onlist:alias}` to
   the registry checksum, canonicalize by **sorting keys and never list values**, and **include the
   read-to-role placement** derived from the reads block. Identical means byte-equal canonical forms.

   Role placement is in there because two technologies differing only in which read is biological would
   otherwise be falsely labelled benign. And list order is significant *because* the sort was once
   applied to values too, which made a spec with its rounds reversed compare identical to itself
   — the finding that deleted the sort is in
   [ADR-0011](../adr/0011-closed-instructable-surface.md).

**The biconditional CI asserts:** backends are identical **if and only if** the declared relationship
is `processing_equivalent`. So v3 versus v3.1 — same module, same CB/UMI positions, same whitelist,
same strand, same role placement — is benign, and `distinguishable_by` is `[none]`. At runtime a score
tie inside a CI-proven equivalent group **must not** escalate: record every id into the chemistry
equivalence class and ask **zero** questions. Non-identical backends imply `processing_divergent`, a
non-empty `distinguishable_by`, and — if that list names `onlist` — a proven onlist separation.
`decidable_by` is derived and asserted equal to the declared list.

An **undeclared** near-tie is never recorded-both. It escalates to a question, so recording both fires
only for a group CI has proven equivalent.

## The round-trip, and the adversarial fixtures

The synthetic generator ([`kb/generate.py`](../../src/seqforge/kb/generate.py)) is a pure function of
the **elements only** — never of `signature` or `backend` — which is what makes the round-trip a real
test instead of a tautology. It walks elements in order: a `barcode` is drawn from a fixed synthetic
cell pool reused across reads so the recurrence signal is realistic, a `umi` is fresh-random, `linker`
and `fixed` are literal, `cdna` comes from a tiny bundled reference, and homopolymers are runs.
Variable and anchored layouts fall out of concatenation.

**Reconcile the cell-pool size with the probe window.** The round-trip and the default probe both
sample the same order of reads, so a pool large enough that every read is unique makes a barcode look
like a UMI, and a pool small enough makes it look constant. The distinct ratio has to land in-band or
the entry proves nothing.

The assertion is `spec → synthetic FASTQ → probe → recovered layout`, and `recovered == declared`.
Then the **adversarial variants, generated from the same block, assert the correct refusal** rather
than a wrong answer:

| variant | expected outcome |
|---|---|
| read reverse-complemented | recovered via the revcomp onlist path, orientation flagged |
| linker with 1–2 mismatches | still recovered |
| barcode read dropped entirely | `Blocker(MISSING_TECHNICAL_READ)` with its remedy |
| a 26 bp R1 against a 28 bp spec | misses the segment-length gate — 28 bp alone does not pick a chemistry |
| fixed-cycle read with more than one distinct length | `Blocker(PRETRIMMED_VARIABLE_LENGTH)` |
| SRA-normalized header | the header test abstains, and does not gate |

`seqforge kb roundtrip` runs this and exits 3 on failure; `seqforge kb lint` validates the schema and
the key allowlist.

## What the KB covers, and what it does not

Recorded so that a green suite is not mistaken for full coverage. The shipped entries were chosen for
**architectural** coverage rather than popularity — breadth before depth:

- **bulk paired-end Illumina RNA-seq** — the no-barcode branch, header parsing, run and lane grouping;
- **10x 3′ GEX v2 / v3 / v3.1, Multiome GEX and ATAC** — onlist matching, technical-read
  identification, SRA mangling, and the benign-twin case;
- **SPLiT-seq** — combinatorial multi-block indexing with fixed linkers and small onlists;
- **BD Rhapsody WTA and Enhanced** — variable-*position* anchored elements, validated against real
  Enhanced reads.

Plus three day-one negatives, which are as much of the coverage as the positives: a truncated gzip
becomes a `Blocker`; a technology absent from the KB becomes `UNSUPPORTED_TECHNOLOGY` rather than a
guess; and metadata contradicting the bytes becomes a surfaced `Conflict`.

**The uncovered case is a variable-*length* barcode** — inDrop's width-varying, W1-anchored cell
barcode. Rhapsody Enhanced's blocks are fixed-width at a floating position, so the anchored-motif
machinery already exists; what is missing is an entry that exercises width variation.

One caveat to carry into a new entry: **there is no dual-derivation check against seqspec**, and a
new entry must not be written as though one will catch a mistake. Every element declares a
`seqspec_region_type` and every read a `seqspec_read_id`, so an export is a pure derivation — but the
emitter is unbuilt, `seqspec` is not a dependency, and nothing here reads its STARsolo output. Whether
that emitter produces position strings for combinatorial or anchored barcodes is therefore not a
scoping question about an existing check; it is a question to answer *when the export is built*. What
does check a new entry is `kb roundtrip`, the params gate, and the confusability sweep.
