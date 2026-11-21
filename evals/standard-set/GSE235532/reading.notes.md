# Notes on the reading of GSE235532

24 samples, 30 passages, 118 claims plus 2 factor claims; all 30 quotes found word for word.
**Read:** the Series record, its 24 sample records, the abstract and main text of pmid:38637528
(no supplement, no figure image). Looked at, not cited: the BioSample and two SRA records.

## What the format could not hold, or held badly

1. **The input pairing rests on one inferred id.** A shared `biosample` id is the only tie between
   a pull-down and its input. It says more than the pairing needs: "same chromatin", also for the
   two marks. One `p` (0.7) covers both beliefs. A study that pools one input per condition could
   not be paired at all. A "control for these libraries" tie, apart from material, is missing.
2. **The archive disagrees and the file cannot show it.** Each of the 24 libraries has its own
   BioSample accession; the reading has 8 biosamples and no slot for the archive's accession.
3. **The mark is a design axis but not a factor.** `target` has no baseline, and inputs have none.
   So replicates and contrasts must also group by `target` and `control_type`; grouped by factor
   levels alone, one input and two pull-downs come out as three replicates.
4. **"Untreated" has no form.** `na` means does not apply, `unknown` means not said. I wrote `na`
   on treatment, dose and duration: three claims for one fact.
5. **Pooled animals.** Each library is 25 heads, so `donor` is `na` with no id. Pool size has no
   slot. A minted donor id per pool would only copy the biosample.
6. **Fly genotype and strain overlap.** w1118 is the control's genotype in the record and, by
   backcross, the mutant's background; `strain: w1118` is inferred. Zygosity is not held. Titles
   name the mutant by stock number 23382; a second name for a level has no slot (see passage 25).
7. **Age has no start point** (presumably since the adult emerged). The fly stage list (FBdv) is
   not a settled term source; I wrote `FBdv:00005369` (adult stage), confirmed at the EBI lookup.
8. **Conditions held constant have no slot:** heads collected at ZT8 (a time of day, in a
   circadian study), mated males, 12 h light and dark.
9. **`target` has no term source.** The antibody is to the asymmetric form of H3R17me2 (passage
   29); its catalogue number has no slot. `healthy` fits a mutant with an aging defect badly.
10. **A paper quote is exact only under a stated text form:** here the XML with tags removed and
    white space collapsed (the allele name is superscript in the XML).

## Choices the format did not fix

- A biosample claim carries `text` (the name three titles share) with `inferred` and `see`. Its
  note repeats on all 24 rows; the full reasoning is one comment above `samples`.
- `control_type: na` sits in `shared` and the 8 input rows replace it; `target` is in every row.
- A paper section is named by its heading path; `read` adds `full_text`, with a comment on scope.

## Format 2 (2026-11-21)

The reading format changed, and the reading was rewritten to it. No claim's text, number, level or
`p` changed.

- `assay` gains the EFO id `EFO:0002692` (ChIP-seq). The file names where its ids came from: the
  EBI's OLS4, where every id was looked up again on 2026-11-21.
- Item 3: the mark stays a label. The check now rejects `target` as a factor.
- Item 4: the format now keeps `na` for a study with no treatment, as this file writes it.
- Item 9: assay now has a term source; target still has none.
