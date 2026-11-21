# Reading notes: GSE241718 (one paper, two Series)

**Read:** both Series records (GSE241718, GSE241275), all 8 sample records, their BioSample, SRA
experiment and BioProject records, the abstract of PMID 38997312, and the full text in the paper
store. 8 samples, 28 passages, 57 claims written (13 shared, 42 in rows, 2 factors). A scratch
script found all 28 quotes byte for byte in the field and records, or paper section, each names.

## What the format could not hold, or held badly

1. **Two sources disagree on one cell.** The four aged sample records say `age: 24 months`; both
   Series designs and the whole paper say 26. A claim holds one `text` and cites only support, so
   the losing quote has no place. I wrote 26 at p 0.55; passage 17 is cited by nothing. Same for
   `cell type: Adipocytes` (18), `10x Genomics` (19) and `scRNA_seq` (20).
2. **One `p` covers level and value.** `aged` is near certain, 26 against 24 a coin flip; 0.55 hides it.
3. **Donor sharing is two questions under one `p`.** The paper says two mice per age group gave
   both depots (about 0.85), not which SAT library pairs with which VAT library (about 0.6). No
   form says "shares a mouse with one of these two", so the donor ids look surer than they are.
   Young SAT is in GSE241718 and young VAT in GSE241275, so donors D1 and D2 each span both Series.
4. **A sample's Series: not needed as a claim, missed in three places.** It is a record fact
   (`Sample_series_id`), and the paper says the split is only two upload stages. But (a) `study:`
   names one Series and nothing lists the other, so I used a comment; (b) `each_sample` fails where
   the two Series word a field differently (`105` against `10^5`), and there is no "each sample of
   Series X"; (c) the Series is confounded with one group (young VAT) and names another sequencer,
   and no slot holds batch or platform, so a derived contrast cannot warn about it.
5. **A factor with no baseline.** The paper compares the two depots with each other: `baseline: null`.
6. **One term slot, two right terms.** `SAT` is subcutaneous adipose tissue by the records and
   inguinal fat pad by the paper. I wrote the deeper term; `text` stays the record's short form.
7. **`see` cannot say a passage speaks against.** The `cell_type` gap now has `see: [8, 13]`, but
   passage 18 contradicts it and only prose says so. One note is repeated on all eight donor claims.
8. **"Word for word" needs a whitespace rule.** The paper writes `2 months` with a no-break space.
   A checker that tidies whitespace passes an inexact quote; mine did at first. See passages 6, 7.
9. **No slot for the library kit** (Singleron GEXSCOPE), and no term source for assay.

**Choices the format did not fix:** untreated animals get `na` for `treatment`, `dose` and
`duration`, not a text "none". Genotype is `wild type`, `inferred`, not `unknown`. The two
GSE241275 rows restate `assay` only to lower its `p`. `format: 1` is the prototype's number.

**Outside the format:** `study.yaml` says one Series is whole cells and one nuclei. The paper and
both protocols say all eight libraries are single-nucleus, two upload stages of one batch.

## Format 2 (2026-11-21)

The reading format changed, and the reading was rewritten to it. No claim's text, number, level or
`p` changed.

- `assay` gains the EFO id `EFO:0009809` (single nucleus RNA sequencing). The file names where
  its ids came from: the EBI's OLS4, where every id was looked up again on 2026-11-21.
- Items 1 and 7: the four aged `age` claims list passage 17 (24 months) under `against`, the shared
  `cell_type` gap lists 18, and the two GSE241275 `assay` claims list 19 and 20. No passage is left
  uncited.
- Item 9: assay now has a term source.
