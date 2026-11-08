# PROTOTYPE, throwaway: handoff

## Done

- The three hand-read studies are read by the model, in four versions, scored and written up in
  `../RESULTS.md`. In each `../runs/<GSE>/`:
  - `reading.v0-asis-*.yaml`: the harness as handed over.
  - `reading.v1.yaml`: the old HANDOFF's missing parts, frozen before the gold YAML was opened.
  - `reading.v2.yaml`: fixes made after seeing the gold. `reading.yaml` is the same file.
  - `reading.v3.yaml`: v2 plus re-asking picks under p 0.9 with options reversed. No answer changed.
  - Also per study: `compare.<version>.json`, `cost.<version>.json`, `answers.<version>.json`.
- New scripts:
  - `compare.py`: scores a reading against the gold, field by field.
  - `cost.py`: cost and time per study, and the 100 and 1,000-sample estimate.
  - `probe_glossary.py`: shows that a donor quote in the state flips a role answer.
  - `fetch.py` also gets the paper's abstract and methods from PMC, kept under `raw/`, which is not
    committed.
- Spend: $0.076 in this unit, $0.116 with the dev study (`calls.jsonl` per run). The $9 stop in
  `common.py` was never near.
- `run.py` now uses DeepSeek's lists by default. The dev study's files in `../runs/GSE201250/` were made
  by the old code-first path (v0). Re-running them would make one new DeepSeek call.

## Left

1. **Give each question its own state.** Donor quotes help the donor question and break the role
   question (`probe_glossary.py`). Better: let code match donor ids that appear as tokens in the title,
   and ask Jev only when code finds none or several.
2. **Check every list item with Jev**, as factor roles are checked now. For example "is `C57/BL6` an
   individual animal?" for donors and "is X an antibody target?" for targets. Add fixed triggers that send
   the writer back:
   - one donor covers every sample;
   - two factors have the same sample partition;
   - more than half the samples are "not stated".
3. **Record mid-range same-material answers as `unsure`** (p_yes about 0.35 to 0.7) instead of no. The
   gold's own unsure pairs fell there.
4. **Re-ask only questions whose answer is used.** 48 of v3's 64 re-asks were speculative control-type
   questions on signal libraries.
5. **Ontology candidates.**
   - Search the head noun and synonyms: "neurons" did not reach CL:0000540.
   - Search the parent disease: "Alzheimer's disease" reached only numbered MONDO subtypes.
   - Map "control" and "normal" to PATO:0000461.
6. **Scale to 1,000 samples** (`../RESULTS.md`, estimate section):
   - Chunk the writer by about 150 sample lines.
   - Past about 280 samples, send the study request a table of distinct values.
   - Shortlist donors in code before the Choice.
7. **Test on unseen studies.** v2's fixes were tuned on these three studies. The next useful test is
   five to ten studies picked blind, scored with `compare.py` after a fresh hand reading. Prefer a large
   study (over 100 samples), a CUT&RUN or IgG study, and a true multiome study, where co-measurement is
   stated.
