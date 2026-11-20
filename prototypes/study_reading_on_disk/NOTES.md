# PROTOTYPE, throwaway: notes on the reading format

GSE100021, from the toy store's records and the abstract of PMID 28645186; a scratch script found
all 21 quotes there. `python derive.py` (PyYAML) rebuilds `relations.derived.yaml`.
`reading.yaml` is 98 lines (94 not blank); its longest line is 293 characters, a quote. It writes
39 claims. Without the `shared` block the same 8 samples need 144.

## Choices beyond the six, and what each beat

1. **The backing key holds its own support:** `quoted: [numbers]`, `inferred: "how"`,
   `unknown: "note"`, `na: "why"`, exactly one per claim. Beat `backing:` + `passages:` + `note:`,
   which is three keys and lets a quoted claim have no passage.
2. **`na` is a fourth mark, and a gap claim has no `text`.** Beat `text: na`: a record can itself
   say "NA" or "unknown", and the two would look the same.
3. **A `shared` block; a sample's own claim replaces the shared one whole.** Beat YAML anchors,
   which vanish when a tool loads and rewrites the file. Beat writing all 144 claims.
4. **Passages are keyed by their number.** Beat a plain list, where the number is the position:
   nobody can see it, and one insert renumbers every claim.
5. **One passage can name many records:** a list, or `each_sample`. Beat one passage per record,
   which turns 9 passages into 64 here.
6. **A quote is one line in double quotes, never folded.** Beat a folded block, which changes
   spaces. One field here has a double space.
7. **`samples` is keyed by GSM.** Beat a list with a `gsm:` field, which can repeat a sample.
8. **`dose` and `duration` are labels beside `treatment`.** Beat nesting them in it: three
   probabilities in one claim, and no one-line form.
9. **`biosample` and `donor` carry an `id` minted in the file;** `text` stays the source's own
   name. Beat putting the minted id in `text`.
10. **Two samples share a level when their `text` is equal** (number and unit for age, dose,
    duration), and `baseline` repeats that text. Beat a separate `level:` key, a second copy.
11. **A `read:` line** says what was read. Beat leaving it out: an `unknown` means little without.

`derive.py` also checks what the format promises: all 18 labels after inheritance, one mark per
claim, each cited passage exists and comes from that sample's record, and a quoted claim's `text`
is found word for word in a cited passage. The last one is not a settled rule; it held here.

## Awkward or unclear in the settled decisions

- **`unknown` is a value in one decision and a backing mark in the other; `na` has no mark.**
  Choice 2 merges them. Confirm or split.
- **What a probability on `unknown` means.** Here: the chance the unread full paper is silent
  too. So `dose` and `duration` get 0.1, which reads as "go and read the paper". The other
  reading (the chance the texts read are silent) is always near 1.
- **A factor has no backing and no probability.** Which label was varied on purpose, and which
  level is the baseline, are judgements, but a claim is one cell of one sample.
- **"Control type" invites a wrong answer.** The paper calls the DsRed rats controls. The slot
  means a background-control library, so all eight are `na`.
- **Treatment fits badly.** Both groups got a vector, so the baseline is a treatment, not none.
  A gene-therapy vector has no term source. The route (into the brain's ventricles) and the
  side (right hippocampus) have no slot and live only in the text. A sample with two treatments
  has no form yet.
- **Equal `text` as the level** breaks when a source spells one level two ways. The writer then
  has to pick one spelling, and `text` is no longer the raw words for every sample.
- **An inferred claim that rests on a passage** can name it only in prose. The same note is then
  written on all eight biosample claims: passages are written once, notes are not.
- **No stage list for rat, and no term source for assay or target.** All three have no term.
- **Not exercised.** One unordered factor, one assay, no control library: only contrasts and
  replicates come out filled. The code for series, shared biosample and signal-to-control pairs
  returns empty lists and is untested.
