# Notes on the reading of GSE100021

Eight samples, one Series, one paper. 39 claims written (15 shared, 24 on samples) in place of 144
after inheritance, plus one factor. A script checked all 29 passages against the records and the
paper: every one is there word for word, and every cited passage number exists.

**Read:** the Series and all eight GEO Samples; the BioSamples, SRA experiments, SRA study and
BioProject they link (these add nothing the GEO records do not say); the stored abstract of PMID
28645186 and the full text of that paper from the stored PDF, which gave the dose, the duration, the
vector construction and the age at the start of the experiment.

## What the format could not hold

- **Route and side.** The vector went into both lateral ventricles and the sequenced tissue is the
  **right** hippocampus. Neither the route nor the side has a slot, and UBERON has no laterality in
  `UBERON:0002421`; both survive only inside `text`, where nothing can match them.
- **A dose given per side.** One `value` plus one `unit` holds one number. "8 µl per side of a
  suspension containing 1.7 × 10¹² viral particles" needed "per side" pushed into the unit word, and
  the total per animal is not representable at all.
- **A quote whose source is a PDF.** The text layer breaks lines inside sentences and hyphenates
  across them, so a paper quote can only be verified after runs of whitespace are collapsed; the
  checker does that. Worse, extraction drops superscripts: the paper reads "1.7 x1012" for
  1.7 × 10¹², so "copy the quote exactly" forces a quote that is wrong as prose, and only the
  `value` field repairs it.
- **No term source** for a gene-therapy vector (ChEBI and UniProt both miss "an adenovector
  expressing rat IGF-I"), for assay or for target, and no stage list for rat, so
  `development_stage: old` carries no term.
- **Repeated prose.** Eight biosample claims carry the same `inferred` note word for word: passages
  are written once, notes are not.
- **The sequenced samples are a subset.** 10 and 12 rats were treated and tested in the maze, four
  per group sequenced; the file covers the eight libraries right and has nowhere to say that.

## Choices the format did not fix

- `factors` is a mapping keyed by the label, not a list, so a label can be a factor only once.
- Levels are named `igf1` and `control_vector`. Both arms were injected, so "no treatment" is not a
  level, and `control_type` stays `na` on every sample.
- `donor.text` takes each sample's description name (IGF1_16, DsRed_7), which looks like the animal
  number; the GEO title (IGF1_1 … DsRed_4) looks like a library number and is written nowhere.
- I added one header key, `read_detail`: `read:` alone does not say which records were read.
