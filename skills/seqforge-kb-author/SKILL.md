---
name: seqforge-kb-author
description: >-
  Author a new knowledge-base technology entry (spec.yaml + README.md +
  fixtures) and prove it with `seqforge kb roundtrip|lint|e2e`. Use
  when adding support for a sequencing chemistry seqforge does not know, or when
  editing an existing spec. The skill drafts prose; the verbs that write and
  verify are deterministic.
---

# seqforge kb-author

```bash
seqforge kb list | show TECH | lint
seqforge kb roundtrip TECH        # spec -> synth -> probe -> recover; recovered == declared?
seqforge kb e2e --workdir DIR     # the real count matrix, vs injected truth
seqforge kb e2e-introns --workdir DIR --assembly ce11   # the GeneFull path
```

## The README is the assay's docs page

`README.md` is rendered into the published site under *Knowledge base → Supported assays* (pulled in
by a snippet include), so write it **for a human reader**: what the assay is, how it's read, how
seqforge tells it apart from its siblings, the common gotchas, and a link to
[scg_lib_structs](https://teichlab.github.io/scg_lib_structs/) where the assay has a page there. The
deep derivations and rationale (strand proofs, benign-twin arguments, offset discriminators) belong
in the heavily-commented `spec.yaml`, which the README points to — not in the README itself. No code
reads the README; it is documentation, and `spec.yaml` is the machine truth.

## Every entry is executable and self-testing

A `spec.yaml` that cannot round-trip is not knowledge, it is a note. `kb roundtrip` gates it, and the
pairwise checks live in the SUITE, not in a verb: `test_no_spec_pair_is_confusable_without_declaring_it`
and `test_section_12_biconditional_holds_over_every_loaded_spec_pair` both collect from
`kb.list_spec_ids()`, so your new spec is covered because it exists, not because someone remembered.

**There is no `kb confusability` verb.** This skill documented one for a year and it was never built —
found 2026-07-16 when the verb guard learned to check subcommands instead of only groups.

**`decidable_by` is DERIVED, do not write one.** It is the union of `distinguishable_by` over your
processing-divergent confusables. It used to be a hand-typed field on every spec that nothing read,
two of them carrying the comment "CI-computed union over the divergent confusables" — no CI computed
it. Declare the confusables honestly and the summary follows.

**A whitelist you declare must be one we ship.**
`test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` checks it: a spec whose decisive
mechanism cannot fire looks exactly like one that works,
right up until a real dataset arrives. SPLiT-seq is the standing example — it names three barcode
lists, we ship none of them, so its three most important tests silently ABSTAIN.

The generator reads **only** `spec.reads` — never `signature` or `backend`. That is what makes the
round-trip a real test rather than a tautology, so do not "help" by teaching the generator about the
signature.

## Never write a value from memory

This is the rule that matters most here, and it is not stylistic. **Look up every one:**

- **EFO CURIEs** — verify against live EFO (EBI OLS). A recalled CURIE is usually plausible and wrong.
- **Read structure, linkers, offsets** — from the primary paper's oligo tables or scg_lib_structs.
  **Pin the kit version.**
- **`soloStrand`** — derive it from the oligo orientation and cite the derivation. Never pattern-match
  from 10x: a wrong strand leaves most reads unassigned while STARsolo exits 0 and emits a matrix
  that merely looks thin. Neither the params gate nor a simulated e2e can catch it — a simulation
  would test your own assumption against itself, which is circular. Real data or a derivation.
- **`soloCBposition`/`soloUMIposition`** — never hand-enter a quadruple. They are derived from the
  element model at compose time.

The SPLiT-seq entry is the worked example: its strand is derived from the paper's Table S12 oligos,
corroborated by the authors' own code, and it records the honest caveat that the field's practice is
weaker than its claim.

## Declare confusables, including the painful ones

`confusable_with` must list entries that share your geometry, with `distinguishable_by`. Under-declaring
fails CI. Two relationships:

- `processing_equivalent` — byte-identical backend params (v3 vs v3.1) → §12 benign: record both,
  ask **zero** questions.
- `processing_divergent` — the answer changes what runs → must be separable by a declared mechanism
  (usually `onlist`), or it becomes exit 4.

Real gotcha: chemistries can differ by an 8 bp offset (SPLiT-seq v1 has Round1 at 86-93; Parse/v2 at
78-85). Published param quadruples disagree in the wild for this reason — different chemistries, not
typos.

After you write `confusable_with`, RUN the check — do not eyeball it:

```bash
pixi run test -- tests/test_kb.py -k "confusable or biconditional"
```

It collects every loaded spec pair and reports any that share your geometry at rungs 0-2 without a
declared relationship. Add each reported edge on BOTH sides (the relation is symmetric). A bare
combinatorial geometry — SPLiT-seq, BD Rhapsody — always reports `bulk-rnaseq-pe`: the generic
paired-end fallback forbids so little it accepts any `cdna + barcode-read` pair, so the edge is
`processing_divergent`, `distinguishable_by: [onlist]`.

## Multi-barcode, linker-delimited chemistries (`CB_UMI_Complex`)

When the cell barcode is SPLIT across several segments separated by fixed linkers — SPLiT-seq's three
ligation rounds, BD Rhapsody's three cell-label blocks — the backend is `soloType: CB_UMI_Complex`,
not `CB_UMI_Simple`. The recipe:

- Model each barcode block as its own `{type: barcode, onlist: <alias>}` element and each spacer as
  `{type: linker, sequence: "<verbatim>"}`. The linker's literal `sequence` is **required** by the
  schema (`Element._addressable`) and is the structural signature — pin it from primary oligos.
- `backend.params.soloCBwhitelist` is an **ordered list** `["{onlist:a}", "{onlist:b}", ...]` in
  **CB-position order**: STARsolo pairs the i-th whitelist with the i-th CB segment.
- **OMIT `soloCBstart`/`soloCBlen` and `soloCBposition`/`soloUMIposition`.** They are DERIVED from the
  element coordinates at compose time — `compose/params.py::derived_params` (the keys in
  `DERIVED_PARAM_KEYS`), emitted in the whitelist's declared order. A hand-entered quadruple is
  the classic silent-wrong value; the derivation exists so you never write one.
- Your `signature.requires` are the fixed linkers (`has_segment ... kind: constant`); each barcode
  block's `onlist_hit_rate` is a `supports`.

Worked example: `src/seqforge/kb/specs/splitseq/spec.yaml` (three 8 bp rounds, two 30 bp linkers) and
`src/seqforge/kb/specs/bd-rhapsody-wta/spec.yaml` (three 9 bp CLS blocks, two linkers, 8 bp UMI).

## Shipping a whitelist (and clearing the debt)

A `CB_UMI_Complex` chemistry almost always needs its barcode lists to **ship**, or it cannot win over
the generic bulk fallback: the two tie `processing_divergent`, decidable only by `onlist`, so with no
whitelist to hit the resolver escalates to a question (exit 4) instead of deciding. `kb roundtrip`
still passes without the list (the generator synthesizes barcodes from `spec.reads`), which is exactly
why the gap is invisible until a real dataset arrives —
`test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` is the tripwire.

```bash
seqforge io onlist pack cls1.txt --name bd-rhapsody-cls1 --uri <authoritative-source> --orientation forward
# repeat per list, then commit the blob + the regenerated index it writes, both under
# src/seqforge/io/onlists/  (bd-rhapsody-cls1.codes.gz and index.json)
```

`io onlist pack` is the ONLY writer of `src/seqforge/io/onlists/index.json`, so it cannot drift from the
blobs beside it. Once a list ships, delete its entry from `UNSHIPPED_ONLIST_DEBT` in
`tests/test_kb.py`. Get the sequences from
an authoritative source and verify against a real dataset — never guess barcodes (a wrong whitelist
exits 0 and emits a matrix that merely looks thin, the same failure shape as a wrong strand).

## Fixed-offset only — check before you author

seqforge models **fixed-offset** elements (a `[start,end)`, or an `anchor` to a motif). A chemistry
whose barcodes sit AFTER a variable-length diversity/phasing insert has non-fixed CB offsets and is
**not** expressible as a fixed `CB_UMI_Complex` today — it needs anchored-element support first. BD
Rhapsody is the live example: the ORIGINAL cell-label bead is fixed-offset (fine), the **Enhanced**
bead adds a variable diversity insert (out of scope). Confirm your target uses the fixed-offset variant
against real R1 bytes before writing offsets.

## `kb e2e` does not cover a new complex chemistry

`kb e2e` / `kb e2e-introns` run the built-in 10x path on sacCer3/ce11; there is no e2e fixture for a
new `CB_UMI_Complex` tech. Its guarantees are therefore: `kb roundtrip` (recovers what it declares),
the params gate (`kb lint` + the `params_gate` disjointness/coverage/faithfulness checks), and the §12
biconditional + under-declaration tests that collect from `kb.list_spec_ids()`. Lean on those, plus one
real dataset — not on a simulated count matrix, which would only test your own assumptions against
themselves.

## No scaffold verb — copy the nearest neighbour

There is no `kb new`. Start a spec by copying the closest existing one — `10x-3p-gex-v3` for a single
contiguous barcode (`CB_UMI_Simple`), `splitseq` or `bd-rhapsody-wta` for a split barcode
(`CB_UMI_Complex`) — then edit and run `seqforge kb lint` (validates against the closed vocabulary:
`ElementType`, `SeqspecRegion`, `Decidable`, and the signature test set, all in `kb/schema.py`) and
`seqforge kb roundtrip <id>`. `kb lint` fails exactly where a typo leaves the vocabulary.
