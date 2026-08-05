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

**`identity.aliases` is load-bearing, and it is read in one direction.**
[`kb/match.py`](../../src/seqforge/kb/match.py) is the only place a prose chemistry string becomes a
node: an alias matches when the *value carries it* (substring, or all of its significant tokens), and
ranking is by specificity ([ADR-0028](../adr/0028-specificity-not-verbosity-ranks-a-chemistry-match.md))
— a naming form beats a describing one, then most alias tokens matched, then a form that entails a
tied rival. So writing an alias is writing a claim that any text carrying it is this chemistry —
`10x 3'` on the family and `10x 3' v3` on the leaf is the pattern, and a bare generic word (`WTA`,
`RNA-seq`) is one you must not add, because it would name a whole field of assays. A value that
carries no alias resolves to nothing, which is the honest answer and a refusal downstream
([ADR-0020](../adr/0020-a-family-term-narrows-it-does-not-conflict.md)).

**`identity.descriptive_aliases` is for a phrase that only describes the run.** The test is whether a
*different* chemistry's record could carry it truthfully: "paired-end RNA-seq" is as true of a
SPLiT-seq library as of a bulk one, so on `bulk-rnaseq` it goes here, while "bulk RNA-seq" — which
no single-cell record says — stays an alias. A descriptive form still reaches its node when the value
names no other, so an archive describing a real bulk record the only way it knows still resolves; it
simply loses to any form that names a chemistry. Ranking by token count alone made the wordier
*description* win, so `SPLiT-seq paired-end RNA-seq` resolved to `bulk-rnaseq` (#266). Both lists
are surface forms for [span verification](../../src/seqforge/harvest/verify.py) — the demotion is in
the ranking only — but **only `aliases` is shown to the extraction model**, so do not put a spelling
here that the model needs in order to name the node at all.

## The schema decisions a field list cannot show

- **An `Element` has exactly one coherent addressing mode** — a fixed `[start, end)` XOR an `anchor`
  (a floating element) XOR `min_len`/`max_len` — enforced by a model validator. `linker` and `fixed`
  elements require a `sequence`, and an open `end: null` is allowed only for `cdna` and `gdna`.
- **The `signature` tests are a closed set, identical to the scorer's evaluators**
  ([`resolve.md`](resolve.md)). `requires` are hard AND-gates and may not use a distinct ratio, which
  is depth-dependent; `supports` are additive positive evidence, and this is where an onlist test and
  a distinct ratio belong; `excludes` are anti-gates, and any pass disqualifies. `read_count` counts
  biological and barcode **roles**, not raw files.
- **`Backend.params` is the chemistry-defining minimum only** — how to *parse* reads. A knob whose
  value is the same for every dataset is the module's, not the KB's and not the recipe's (below). The
  one interpolation token allowed anywhere in it is `{onlist:<alias>}`, and it is validated: any other
  `{…}` fails.
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
`clipAdapterType CellRanger4`, `outFilterScoreMin 30`, and `soloCellFilter EmptyDrops_CR` beside them —
are not chemistry, so they are not the KB's. They are not the recipe's either: their value varies with
*nothing*, so they are **literals in `starsolo.smk`'s shell block**
([ADR-0022](../adr/0022-three-owners-for-an-aligner-param.md)). The parse-versus-count key split that
makes "a user instruction contradicts the bytes" inexpressible is
[ADR-0011](../adr/0011-closed-instructable-surface.md).

**`soloCBmatchWLtype` is the KB's, and it is the useful edge case.** Its 10x value
(`1MM_multi_Nbase_pseudocounts`) was chosen for CellRanger parity exactly like the five above — but
unlike them it *varies with the chemistry* (`1MM` for BD Rhapsody and SPLiT-seq, `EditDist_2` for
Parse Evercode), and for a `CB_UMI_Complex` spec STAR rejects its own global default, so reads cannot
be parsed without it. Owner is decided by what the value varies with, never by what it is for.

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

A whitelist may legitimately be **pinned ahead of the spec that will need it** — packing it is a
separate, verifiable act from authoring the entry, and parking the value in the tracker instead invites
the next author to look it up again or, worse, to assert it. That is how `3M-3pgex-may-2023` and
`3M-5pgex-jan-2023` shipped. Both now have their entries (`10x-gemx-3p-v4`, `10x-5p-gex-v3`), and this
section held their values only until they did; each measured separation lives in the spec that stands
on it. **A pin is a loan, not a home.** If one is sitting here with no spec, that is a debt, and
`test_every_confusable_target_is_a_technology_we_support` is what stops the corresponding
`confusable_with` edge from pretending otherwise.

## The 5′ entries, and what they cost

Both are shipped (`10x-5p-gex-v2` covering v1+v2, `10x-5p-gex-v3`, under the `10x-5p-gex` family), and
two things about them generalize.

**`soloStrand: Reverse`, derived from the kit's own oligos rather than from practice.** This entry was
blocked for exactly one reason — the available evidence was community consensus, and STAR's manual
takes no position on 5′ — and the block lifted when the derivation was done at the same bar
`splitseq`'s strand met. It is written out in full in `10x-5p-gex-v2/spec.yaml` and its README, with a
**known-answer control**: run the identical derivation on the 3′ appendix and it reproduces `Forward`,
which every 3′ entry already carries. A derivation that cannot reproduce an answer you already trust is
not a derivation. Two caveats are recorded there so they are not rediscovered as bugs: `Forward` is
correct for `SC5P-PE` / `SC5P-R1`, which this KB **cannot express** (the starsolo parse namespace
allows neither `soloBarcodeMate` nor `clip5pNbases`), so `Reverse` holds unconditionally here.

**"5′ is decidable only by metadata" is a claim about a version PAIRING.** v1/v2 reuse
`737K-august-2016`, which is 3′ v2's list, so that pair shares geometry *and* whitelist and is the one
genuinely read-undecidable pair in the KB — `[metadata, alignment]`, and it is declared on the
`10x-3p-gex-v2` ↔ `10x-5p-gex-v2` edge where it is true. v3 has its own `3M-5pgex-jan-2023`
(3 686 400 × 16 bp), sharing 0.62% with `3M-february-2018`, 6.87% with `3M-3pgex-may-2023` and 0.00%
with `737K-arc-v1` — all far under the 0.6 gate, so all four of its edges into the 28 bp cohort are
`[onlist]`. v3 and v3.1 previously declared a blanket `[metadata, alignment]` edge against the bare id
`10x-5p-gex`, wrong on both counts: wrong partner (their 28 bp neighbour is 5′ v3, not 5′ v1/v2) and
wrong mechanism.

**The cost is a case that now asks, and that is the point.** `10x-v2-bytes-only` graded `decide` before
these entries existed, and that decision was correct only because the alternative had no spec: a real
5′ v1/v2 library resolved silently to 3′ v2 and compiled `Forward`, at exit 0, with nothing red. A
published both-ways run (AlexsLemonade/alsf-scpca #137) measured 0.5–0.6 gene-expression correlation
under the wrong orientation against >0.98 under the right one. Adding an entry that makes an existing
answer into a question is a *net gain* whenever the old answer was a coin-flip; the corpus now carries
both halves — the `ask`, and `10x-5p-gex-v2-metadata-decided` proving the declared mechanism fires.

## Two worked entries

**`10x-3p-gex-v3`** is the fixed-offset case: R1 is 28 bp of 16 bp CB plus 12 bp UMI
(`soloType CB_UMI_Simple`), R2 is open-ended cDNA. Its `signature` shows the rung structure clearly —
`requires` are structural gates friendly to the cheap rungs (read count, 28 bp segment length, two
random segments; **no** onlist and **no** distinct ratio), `supports` add the onlist hit rate that
costs a rung-3 lookup plus depth-dependent distinct-ratio priors, and `excludes` anti-gate the
Multiome whitelist.

Its `confusable_with` block is the load-bearing part. v3.1 is `processing_equivalent` with
`distinguishable_by: [none]` — identical geometry, whitelist and params, so a tie between them is
benign and asks zero questions. Multiome, GEM-X 3′ v4 and 5′ v3 all share the same 28 bp / 16+12
geometry and are `processing_divergent`, separated only by an onlist at rung 3 — which is why a GEM-X
entry is *required* for the flagship to pass its own under-declaration check. Four chemistries, one
geometry, told apart pairwise by which of four whitelists hits.

The read-undecidable case is one generation earlier: **3′ v2 versus 5′ v1/v2**, which share the 26 bp
geometry *and* `737K-august-2016`, and are therefore `[metadata, alignment]`. It is the KB's only such
pair, and worth knowing where it lives — a mechanism list is a claim about which rung can answer, and
putting it on the wrong edge makes the resolver ask a human a question rung 3 could have settled.

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
- **Pin the read structure to a citable source, verbatim — then check it against reads.** SPLiT-seq's
  layout and both linker sequences are taken from
  [scg_lib_structs](https://teichlab.github.io/scg_lib_structs/methods_html/SPLiT-seq.html) (CC-BY),
  not from memory. That is the floor, not the ceiling: base 8 of `linker1` is **`C`** in 90.3% of real
  GSE110823 reads and the published `A` appears in 0.9%, below the sequencing-error floor — and Table
  S12, scg_lib_structs and the authors' own pipeline all carry the `A`. Three independent citations
  agreed with each other and disagreed with the instrument. A citable source beats memory; real reads
  beat a citable source, and the only reason this survived is that nothing greps the linker (our gate
  measures *constancy*, the authors' code uses their linker string only to locate offsets).

The `anchor` path — a floating element whose frame is recovered per read — is exercised instead by
**BD Rhapsody Enhanced**, whose 0–3 bp diversity insert floats every CB block and whose motif-anchored
resolver ([`kb/anchor.py`](../../src/seqforge/kb/anchor.py)) phase-locks the `GTGA`/`GACA` linkers to
recover the frame. A read whose frame is not found contributes nothing, rather than a wrong window.

## The confusability matrix, computed and not written down

For every ordered pair of specs, CI derives three facts and then validates the declared labels against
them. There is no hand-maintained truth table.

1. **Could either spec outrank the other at the cheap rungs?** Generate synthetic reads from B's
   declared layout, score **both** A and B against them with the onlist withheld, and compare —
   then symmetrically. **Under-declaration is a CI error:** A could outrank B on B's own reads, and B
   is not in A's `confusable_with`. That is the guard that makes a GEM-X entry mandatory once the
   flagship exists. **Over-declaration is a CI warning:** separable, but declared confusable anyway.
   The question is an **ordering** one and used to be a validity one
   ([ADR-0029](../adr/0029-a-spec-declares-read-sets-not-a-fixed-read-list.md)): validity tracked
   danger only while every spec consumed every file, and a spec that consumes fewer is valid against
   nearly every leaf while scoring far below all of them, so the guard would have demanded an edge
   from that spec to almost the whole KB. `resolve/confuse.py` holds the predicate and
   `rung02_margin` the number under it; bulk's five edges are re-derived from that margin in
   `tests/test_kb.py`, because the sweep itself skips a declared pair and so cannot notice one that
   stopped being true.
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

**Families declare too.** The sweep above runs over leaves, because only leaves are scored at runtime;
an abstract family is checked by the recognition self-test instead, and its rule is the same one a
level up — a family may recognise a leaf outside itself only if it *says* so, naming that leaf or one
of its ancestors in `confusable_with`. That used to be a flat "reject every non-descendant", and its
own comment recorded why it held: the 26–28 bp R1 gate did all the work, "no cross-family edge needed".
`10x-5p-gex` is the case that needs one — 5′ reads *are* 3′ reads to every cheap probe, so no gate can
separate the families that would not also reject the family's own children. Recognition is not the
thing to forbid; **undeclared** recognition is. `bulk-rnaseq` is declared by nobody, so the original
accepts-everything trap still turns the test red.

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
| fixed-cycle read with a minority of reads still at the declared length | `Blocker(PRETRIMMED_VARIABLE_LENGTH)` |
| SRA-normalized header | the header test abstains, and does not gate |

`seqforge kb roundtrip` runs this and exits 3 on failure; `seqforge kb lint` validates the schema and
the key allowlist.

**A declared constant sequence is read back, not merely measured** (#285). The round-trip recorded a
check for onlist-backed barcodes and for UMIs while computing a statistic for every element, so a
`linker`/`fixed` one fell straight through: six checks ran for `splitseq` and *none* touched its two
30 bp linkers — the sequences that entry's whole discipline rests on, and where three published
sources turned out to disagree with the instrument at base 8. Each one is now cut back out of the
generated reads and compared base for base against what the spec says, over a fixed `[start, end)` and
over a recovered anchor frame alike, which is what closes SPLiT-seq and both BD Rhapsody Enhanced
entries with one check. What can genuinely fail is the two derivations of *where* the sequence goes
disagreeing — the generator concatenates elements in order, the check cuts the declared coordinates,
and nothing validates that a `sequence`'s length matches its own window, so a typo'd linker shifts
everything after it and lands here. On the anchored path the claim is weaker by construction (the
frame is found *by* matching the linker), which is why the demonstration that the check can fail picks
a fixed-coordinate element.

**A `min_rate` is a frequency, and the generator writes every element on every read** — so each
entry's structure is in 100 % of its own reads and every declared motif floor was tested infinitely
far above itself. The floor test builds the population the entry actually claims: the spec's own reads
mixed with the all-cDNA entry's, the honest diluent since one generator draws both. It asserts the
gate PASSES a quarter above the floor and FAILS a quarter below it, and both sides are needed —
PASS alone is what the 100 %-tagged fixture already gave, and FAIL alone would pass for a gate that
can never fire. The diluent is *derived* (the one entry whose every element is plain cDNA) rather than
named, which is the difference between a test that follows a rename and one that breaks on it.
**Its limit, written down so a green is not over-read:** synthetic cDNA is uniform random, while real
untagged reads of the chemistry that motivated this carry the tag off-offset at ~6 % and *structured*,
at offsets 13/15/23, against 0.25 % in real bulk. This calibrates a gate against a FREQUENCY;
robustness against that structured background stays a measurement on real reads and is not claimed
here.

Both live in `tests/test_kb.py`, generic over every shipped entry, and neither names a spec.

## What the KB covers, and what it does not

Recorded so that a green suite is not mistaken for full coverage. The shipped entries were chosen for
**architectural** coverage rather than popularity — breadth before depth:

- **bulk paired-end Illumina RNA-seq** — the no-barcode branch, header parsing, run and lane grouping;
- **10x 3′ GEX v2 / v3 / v3.1, GEM-X v4, Multiome GEX and ATAC** — onlist matching, technical-read
  identification, SRA mangling, and the benign-twin case;
- **10x 5′ GEX v1/v2 and v3** — the *read-undecidable* branch: a pair that shares geometry AND
  whitelist, so the resolver must reach past the bytes for metadata or an alignment rather than pick;
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
