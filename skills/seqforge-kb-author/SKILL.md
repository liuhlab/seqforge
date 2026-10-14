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

## Start from the nearest neighbour

There is no `kb new` scaffold. Copy the closest spec — `10x-3p-gex-v3` for one contiguous barcode
(`CB_UMI_Simple`), `splitseq` or `bd-rhapsody-wta` for a split barcode (`CB_UMI_Complex`) — then edit
and run `seqforge kb lint` (the closed vocabulary in `kb/schema.py`: `ElementType`, `SeqspecRegion`,
`Decidable`, the signature test set; it fails exactly where a typo leaves it) and
`seqforge kb roundtrip <id>`.

**Fixed-offset only.** seqforge models elements at a `[start,end)` or anchored to a motif. Barcodes
after a variable-length diversity/phasing insert have no fixed CB offset and need anchored-element
support first. BD Rhapsody: the original cell-label bead is fixed-offset; the **Enhanced** bead adds
a variable insert and is out of scope. Confirm the fixed-offset variant against real R1 bytes before
writing offsets.

## The README is the assay's docs page

`README.md` is snippet-included into the site under *Knowledge base → Supported assays*, so write it
for a human: what the assay is, how it is read, how seqforge tells it from its siblings, the common
gotchas, and a link to [scg_lib_structs](https://teichlab.github.io/scg_lib_structs/) where the assay
has a page. Derivations and rationale (strand proofs, benign-twin arguments, offset discriminators) go
in the heavily commented `spec.yaml`, which the README points to. No code reads the README;
`spec.yaml` is the machine truth.

## Every entry is executable and self-testing

`kb roundtrip` gates each spec. The pairwise checks live in the suite and collect from
`kb.list_spec_ids()` — `test_no_spec_pair_is_confusable_without_declaring_it` and
`test_the_benign_twin_biconditional_holds_over_every_loaded_spec_pair` — so a new spec is covered because it
exists. There is no `kb confusability` verb.

- **`decidable_by` is derived; write none.** It is the union of `distinguishable_by` over the
  processing-divergent confusables. Declare those honestly and it follows.
- **The generator reads only `spec.reads`**, never `signature` or `backend`. That keeps the round-trip
  a real test; leave the generator ignorant of the signature.
- **A `linker`/`fixed` sequence's length must equal its window.** The round-trip cuts each constant at
  its `[start, end)` (or the anchored frame) and compares base for base; a sequence one base off
  shifts every later element and usually reddens on the *next* linker. Count the bases.
- **A `motif_present` `min_rate` is a frequency, tested at its floor** against populations mixed just
  under and just over it. Pick a rate you would defend on a part-tagged library. A floor at or below
  the motif's chance rate in plain cDNA cannot fail.

## Look up every value; write none from memory

- **EFO CURIEs**: verify against live EFO (EBI OLS). A recalled CURIE is usually plausible and wrong.
- **Read structure, linkers, offsets**: from the primary paper's oligo tables or scg_lib_structs.
  **Pin the kit version.**
- **`soloStrand`**: derive it from oligo orientation and cite the derivation; never pattern-match from
  10x. A wrong strand leaves most reads unassigned while STARsolo exits 0 with a thin-looking matrix.
  Neither the params gate nor a simulated e2e catches it (a simulation tests your assumption against
  itself). Real data or a derivation.
- **`soloCBposition`/`soloUMIposition`**: derived from the element model at compose time; never
  hand-enter a quadruple.

Worked example: SPLiT-seq's strand comes from the paper's Table S12 oligos, is corroborated by the
authors' code, and records the caveat that field practice is weaker than the claim.

## Declare confusables, including the painful ones

`confusable_with` lists every entry sharing your geometry, with `distinguishable_by`; under-declaring
fails CI.

- `processing_equivalent` — byte-identical backend params (v3 vs v3.1): benign, record both, ask
  **zero** questions.
- `processing_divergent` — the answer changes what runs: must be separable by a declared mechanism
  (usually `onlist`), or it becomes exit 4.

Chemistries can differ by an 8 bp offset (SPLiT-seq v1 Round1 at 86-93, Parse/v2 at 78-85), which is
why published param quadruples disagree in the wild.

After writing `confusable_with`, run the check:

```bash
pixi run test -- tests/test_kb.py -k "confusable or biconditional"
```

It reports every loaded spec pair sharing your geometry at rungs 0-2 without a declared relationship.
Add each edge on BOTH sides. A bare combinatorial geometry (SPLiT-seq, BD Rhapsody) always reports
`bulk-rnaseq`, whose generic paired-end fallback accepts any `cdna + barcode-read` pair: that edge is
`processing_divergent`, `distinguishable_by: [onlist]`.

## Split barcodes (`CB_UMI_Complex`)

When the cell barcode is split across segments separated by fixed linkers (SPLiT-seq's three ligation
rounds, BD Rhapsody's three cell-label blocks), the backend is `soloType: CB_UMI_Complex`:

- Each barcode block is a `{type: barcode, onlist: <alias>}` element; each spacer is
  `{type: linker, sequence: "<verbatim>"}`. The schema (`Element._addressable`) requires the literal
  `sequence`, the structural signature — pin it from primary oligos. With a fixed `[start, end)` too,
  it requires `len(sequence) == end - start`.
- `backend.params.soloCBwhitelist` is an **ordered list** `["{onlist:a}", "{onlist:b}", ...]` in
  **CB-position order**: STARsolo pairs the i-th whitelist with the i-th CB segment.
- **Omit `soloCBstart`/`soloCBlen` and `soloCBposition`/`soloUMIposition`.** They are derived from
  element coordinates at compose time (`compose/params.py::derived_params`, keys in
  `DERIVED_PARAM_KEYS`), in the whitelist's order.
- `signature.requires` holds the fixed linkers (`has_segment ... kind: constant`); each block's
  `onlist_hit_rate` is a `supports`.

Worked examples: `src/seqforge/kb/specs/splitseq/spec.yaml` (three 8 bp rounds, two 30 bp linkers),
`src/seqforge/kb/specs/bd-rhapsody-wta/spec.yaml` (three 9 bp CLS blocks, two linkers, 8 bp UMI).

`kb e2e` / `kb e2e-introns` run only the built-in 10x path on sacCer3/ce11, with no fixture for a new
complex tech. Its proof is `kb roundtrip`, the params gate (`kb lint` + the `params_gate`
disjointness/coverage/faithfulness checks), the suite's pairwise tests, and one real dataset — never a
simulated count matrix.

## Ship the whitelist with the spec

A spec that names the onlist decisive lands only with its whitelist shipped; there is no deferral.
`test_a_spec_that_calls_onlists_decisive_can_actually_reach_one` stays red until it ships. Without the
list `kb roundtrip` still passes, but on real reads the chemistry scores 0.3300 against
`bulk-rnaseq`'s 0.7800, far outside the tie band, so nothing asks and the deposit compiles to a bulk
gene-count matrix at exit 0. SPLiT-seq names three lists and ships none, so its three most important
tests ABSTAIN.

```bash
seqforge io onlist pack cls1.txt --name bd-rhapsody-cls1 --uri <authoritative-source> --orientation forward
# repeat per list, then commit the blob + the regenerated index it writes, both under
# src/seqforge/io/onlists/  (bd-rhapsody-cls1.codes.gz and index.json)
```

`io onlist pack` is the only writer of `src/seqforge/io/onlists/index.json`. Take sequences from an
authoritative source and verify against a real dataset: a wrong whitelist, like a wrong strand, exits
0 with a thin-looking matrix.
