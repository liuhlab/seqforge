# 3. The manifest is the interface SRAgent and scRecounter never had

SRAgent reads an accession's prose into structured metadata, and scRecounter distrusts SRA metadata
entirely — it grid-searches STAR parameters over real alignments to re-derive facts SRAgent had
already read — so the obvious fix is a third pipeline wiring the two together. It loses because the
gap is a missing *interface*, not a missing feature: `manifest.yaml` decides once, span-verified and
hashed, and an argmax over parameter combinations has no way to say "I don't know", where an
undecidable dataset must yield a `Blocker` and a nonzero exit. Alignment survives only as escalation
rung 6, on an ambiguity code has already flagged.
