# GSE112068: notes on the reading

**Read:** the Series record and all 23 sample records in the toy store (build `20261107T163933Z-subset`). No paper is linked. The BioProject and the 23 BioSample records add nothing. **Written:** 23 samples, 45 passages, 91 claims and 2 factors (437 claims before `shared`). A scratch script found all 45 quotes word for word in the store, in every record each passage names.

## What the format could not hold, or held badly

1. **Two times, one `duration` slot.** The cells got bleomycin for 2 h and were harvested 2 h to 8 days later. I put the exposure length in `duration` and made the harvest time a study-specific attribute, `time_point`, which is the second factor. The prototype used `duration` the other way (injection to dissection). ENCODE has two fields: `duration` and `post_treatment_time`. The format needs a rule, or a second slot.
2. **A value and a unit do not say what the time is measured from.** "2 h after exposure" to a 2 h exposure may count from its start or its end. "16 weeks gestation" becomes 16 and `week`, and "gestation" stays only in `text`.
3. **An ordered factor in mixed units.** The levels run 2 h … 36 h, then 2 days … 8 days. Value and unit cannot be sorted without a unit table, so the order lives only in the `levels` list. I assumed the list order is the order; the format does not say so.
4. **Nothing says two factors are crossed, or that the cross is not full.** Dose 0 has 3 of the 11 times, and time zero exists only at dose 0. It can be recomputed from the rows, but one `baseline` per factor is wrong here: 16 of the 20 exposed samples have no unexposed sample at their own time, and the format cannot say that GSM3056966 is time zero for all three dose series.
5. **A disagreement between records has no place.** The protocol says "12 time points", lists 11, and names "0 h" and "32 h". The samples have 10 times per dose, "36 h", and no 0 h. A claim can cite only passages that support it. I gave the two 36 h claims p 0.9 and the time factor p 0.85.
6. **An untreated sample has no settled form.** I wrote `treatment: control` (the record's `agent: control`). With `na` the quote is lost and "dose: 0 ug/ml" hangs on nothing. Agent and dose are one fact written twice, so only `dose` is a factor. A vehicle or a mock exposure has no slot; `duration: na` on the controls is a guess (p 0.7).
7. **One p on a factor** covers the label, the levels, the baseline and the order together.
8. **A cell line strains several slots.** Age, tissue and donor describe the fetus the line came from, not the dish. Sex is `na` because the attribute rule's own example says so, although the donor's sex is a known fact outside the records.
9. **Cellosaurus is not in the EBI lookup service,** so `CVCL_0347` is from the model's memory and unconfirmed. The other six terms were confirmed there.
10. **A passage that covers "all but three samples"** needs a 20-accession list. The longest line is 318 characters.
11. **The biosample note is repeated 23 times.** `see:` moved the passage out of it, but "one dish per library" cannot be said once.

## Choices the format did not fix

- `shared` works as a default: `treatment` and `duration` hold for the 20 exposed samples and the 3 control rows replace them. The rule allows it; the name does not suggest it. It saves 40 claims.
- One donor, `D1`, for all 23, in `shared`. One biosample per library, inferred.
- Units: `hour`, `day`, `week`, and `ug/ml` in the record's spelling. No unit list is fixed yet.
- Level names start with a digit (`0ug`, `36h`, `2d`). YAML reads them as text; a bare `0` or `2` would load as a number.
- `baseline` holds a level name, not the source's text.
- A passage may be part of a field (one sentence of the protocol), and a Series passage may back a sample claim.

## Format 2 (2026-11-21)

The reading format changed, and the reading was rewritten to it. No claim's text, number, level or
`p` changed.

- `assay` gains the EFO id `EFO:0008896` (RNA-Seq). The file names where its ids came from: the
  EBI's OLS4 on 2026-11-21. `CVCL_0347` is still from memory, since OLS4 does not hold Cellosaurus.
- Item 5: the two 36 h claims now list passage 13, the protocol's "32 h", under `against`.
- Item 6: an untreated sample in a study that treats now writes its text. `treatment: control` on
  the three unexposed rows is that form, so the verdict's suggested `na` is no longer allowed by the
  format. The claims are unchanged.
