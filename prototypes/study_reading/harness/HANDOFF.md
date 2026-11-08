# PROTOTYPE, throwaway: handoff

## Works

- `run.py <GSE>` reads one study end to end into `../runs/<GSE>/reading.yaml` (SCHEMA.md shape);
  cached, so a re-run calls no model. Dev study GSE201250 done, probes and estimate done; results
  and verdict in `README.md`.
- Spend so far: about $0.04 of the $10 cap (`calls.jsonl` per run; `common.py` stops at $9).

## Left

1. Run the three hand-read studies (accessions in `../gold/STUDIES.txt`):
   `pixi run python run.py <GSE>` for each, then `--writer only` to try DeepSeek's lists.
   Expect breakage the dev study never hit:
   - ChIP-seq: the target (`target` is always null now) needs a question: a Choice among targets the
     writer lists, per sample. `control_type` is asked speculatively already.
   - Co-measured assays: biosample pairs are asked only inside code blocks of samples equal on every
     key. A multiome pair differs on assay, so add assay-blind blocking (same donor and same factor
     levels) before asking yes/no.
   - SuperSeries: `fetch.py` follows SubSeries but the writer prompt grows with every sample line;
     past about 60–70 samples, send a table of distinct characteristic values, not every sample.
   - The study request's state is `study_text()` with every sample; same fix past ~100 samples.
2. Add the study glossary (each level with its defining quote) to the per-sample state; the
   no-characteristics genotype errors came from missing it (README, probe 2+3).
3. Compare with the gold field by field (a small script over the two YAML files) and report
   accuracy, calibration and abstention per field.
