# THROWAWAY — the measurement half of wayfinder #234

Map [#225](https://github.com/liuhlab/seqforge/issues/225) forbids production code under this effort.
Nothing here is imported by `src/seqforge`, nothing here ships in the wheel, and the branch
(`prototype/smartseq3-byte-separation`) is not meant to be merged. It exists so the maintainer can
read numbers instead of a proposal, and so anyone can re-run them.

**The question:** can the bytes separate Smart-seq3 from `bulk-rnaseq-pe`, and does it cost a new
word in the signature DSL? Today all ten packaged GSE207085 cells FALSE ACCEPT as `bulk-rnaseq-pe`
at exit 0 ([#230](https://github.com/liuhlab/seqforge/issues/230)).

## Inputs

- ten fingerprint packages from `liuhlab/seqforge-benchmark` `packages/GSE207085/` (#230), unpacked
  under a scratch directory the scripts point at with `SP`;
- `GSE283483-bulk.fingerprint.tar.gz` as the **bulk control** — the benchmark case that resolves to
  `bulk-rnaseq-pe`. A signal only ever measured on positives proves nothing;
- the shipped KB and the whole eval corpus, for the regression check.

Every script takes its paths from module-level constants at the top. `SP` is a local scratch path and
will need editing on another machine.

## What each file measures

| file | measures |
|---|---|
| `measure_offsets.py` + `report_offsets.py` | per-cell tag fraction and the full OFFSET HISTOGRAM, on R1, R2, the bulk control, the Tn5 mosaic end (a non-positional motif control) and five random 11-mers (the empirical chance floor) |
| `background.py` | where the off-offset tag hits come from — per cell, per offset, and for the reverse-complement tag |
| `run_evaluators.py` | RUNS every existing evaluator in `resolve/evaluators.py` against SS3 R1 / SS3 R2 / bulk, and prints which ones SEPARATE |
| `positional_concentration.py` | the PROPOSED new evaluator, written to the shape `evaluators.py` would need, plus its floor against a dilution series |
| `floor.py` | the dilution series on its own: real tagged reads padded with real untagged Smart-seq3 reads (the honest diluent) and with real bulk reads |
| `spec-draft/smartseq3/spec.yaml` | the draft KB entry. **Not** under `src/seqforge/kb/specs/` |
| `confuse_and_score.py` | `rung02_separable` both directions over every KB spec, `backend_identical`, a Spec-taking copy of `kb roundtrip`, and the ten cells + bulk resolved under stock / draft-R / draft-S |
| `regress.py` + `analyze_regression.py` + `margins.py` | THE REGRESSION CHECK: every case in both eval tiers, scored three ways, diffed on the full ranked candidate list |
| `single_end_and_question.py` | the question the shallowest cell asks, and what a single-end library does |
| `ci_with_draft.sh`, `ci_kb.sh` | temporarily install the draft into the real KB, run the CI gates that police an entry, remove it. Nothing is committed under `src/` |

`results/` holds the JSON each script wrote.

## Running them

The scripts put the repo's `src/` first on `sys.path`; the shell scripts export `PYTHONPATH`.
That matters here — the pixi env carries an editable install pointing at the *main* checkout, so a
worktree's edits are invisible without it, and a CI run that silently tested the wrong tree is exactly
the failure this note exists to prevent.

```
<env>/bin/python prototype/smartseq3/measure_offsets.py
<env>/bin/python prototype/smartseq3/report_offsets.py
<env>/bin/python prototype/smartseq3/run_evaluators.py
<env>/bin/python prototype/smartseq3/positional_concentration.py
<env>/bin/python prototype/smartseq3/confuse_and_score.py
<env>/bin/python prototype/smartseq3/regress.py && \
<env>/bin/python prototype/smartseq3/analyze_regression.py
bash prototype/smartseq3/ci_with_draft.sh
```

The findings are posted on #234. The maintainer decides; this branch decides nothing.
