# PROTOTYPE, throwaway: DeepSeek writes the lists, Jev answers the closed questions

Question: what can Jev check well on a real study, and what can it not? Dev study: **GSE201250**
(bulk RNA-seq, 15 samples: monocyte-derived dendritic cells from 3 blood donors, shRNA knockdown of
MDA5 or a luciferase control, HIV-1 or mock, 0 h or 48 h). Every claim below is marked
**measured** (run here, numbers in `../runs/GSE201250/`), **read** (TypeSafe or DeepSeek docs) or
**inferred**.

## Run it

Three real studies are now read too; results, errors and costs are in `../RESULTS.md`.

```bash
cd prototypes/study_reading/harness
pixi run python run.py GSE190102 --label v2       # fetch (GEO + paper from PMC), DeepSeek lists, Jev, relations
                                                  # -> ../runs/<GSE>/reading.yaml, answers.json, cost.v2.json
REASK=1 pixi run python run.py GSE190102 --label v3   # also re-ask every pick under p 0.9 with options reversed
pixi run python compare.py all reading.v2.yaml    # score against ../gold/ -> ../runs/<GSE>/compare.v2.json
pixi run python compare.py --table                # v0, v1, v2 side by side, per study
pixi run python cost.py v1 v2 v3                  # cost and time per study; estimate for 100 and 1,000 samples
pixi run python probe_glossary.py                 # does a donor quote in the state move an answer?
pixi run python probes.py GSE201250               # the dev study's five probes -> probes.json
```

Needs `DEEPSEEK_API_KEY` and `TYPESAFE_API_KEY` in the environment. Every model response is cached
under `runs/<GSE>/cache/`, keyed by the exact request (option order included); a re-run makes no
model call (**measured**: 0 live calls on the second run). Live calls are logged to
`runs/<GSE>/calls.jsonl` (model, tokens in and out, dollars, seconds, tag, harness version).

| Script | Does |
|---|---|
| `fetch.py` | GEO Series and sample text over public HTTP, SubSeries followed, submitter contact lines dropped; the paper's abstract and methods from PMC under `raw/` (not committed); `study.json` |
| `draft.py` | open lists from DeepSeek (`deepseek-chat`): factors, targets, controls, donors, biosample groups, assays, attributes, each with a quote grepped back to GEO or the paper. `draft(gse, "auto")` is the dev study's code-first path (v0) |
| `ask.py` | closed questions to `jev-1.14.0`: one study request (each factor's role and baseline, ontology terms among OLS candidates, sample type, one individual per sample), one request per sample (level per factor, donor, target, assay, role, control type, varying attributes) with a study glossary in the state, then same-material yes/no for pairs code blocks by donor and factor levels |
| `derive.py` | design table and the five relations, in code, scoped by assay, target and role; `reading.yaml` |
| `compare.py` | field-by-field score against `../gold/`, Jev's probability beside each right or wrong |
| `cost.py` | cost and time per study and version; the 100 and 1,000-sample estimate |
| `probes.py`, `estimate.py` | the dev study's probes and estimate (GSE201250 only) |

Schema additions in model readings: sample `title`, `series` and `unsure` (labels whose re-ask disagreed),
as in the gold; factor `probability` (role, baseline); `probability.donor_study` when every sample is its own
donor by the study question; an attribute's `source` when it is not a GSM characteristic.

## Result on the dev study

**Measured.** Both list sources gave the same design table: factors genotype (baseline control),
infection (baseline Mock), time (baseline 0h, ordered); donor key "donor number" classed as donor,
not factor (p 1.0); cell type mapped to CL:0011031 (p 0.97); 5 contrasts, 5 replicate groups, 1
series, no co-measurement, no background control. That matches my own reading of the GEO page.
DeepSeek's lists: 12 quotes, 11 grep back; the failure quoted a label the prompt added
(`organism: Homo sapiens`), not GEO text.

Main path cost: 1 DeepSeek call (2,192 tokens in, 308 out, $0.0010, 1.9 s, skipped by default here
because characteristics list the levels) and 16 Jev requests (23,365 tokens, $0.0010, 0.33 s
slowest, under 1 s wall at 8 in parallel). All runs and probes together: 1,016 Jev requests,
$0.039; 2 DeepSeek calls, $0.0014.

## Probes (all measured, Jev 1.14.0, GSE201250)

"No chars" means the sample's record without its typed characteristics: title, source, organism,
treatment protocol. That is the hard case, a study whose characteristics do not carry the design.

**1. Option order.** 187 Choice questions × 4 orders (original, reversed, two shuffles).

| Order | Accuracy | Picked the first option | Answers changed vs original |
|---|---|---|---|
| original | 0.995 | 0.63 (truth often listed first) | - |
| reversed | 1.000 | 0.00 | 1 |
| shuffle 1 | 0.984 | 0.28 | 2 |
| shuffle 2 | 0.995 | 0.32 | 0 |

3 of 187 questions changed under some order; all 3 were genotype from no-chars records, all with
top probability below 0.72. No lean toward the first option showed on confident questions.

**2+3. Question size and context.** 60 labels: 3 factors + donor for 15 samples.

| Variant | Requests | Questions | Tokens in | Accuracy (with chars / no chars) | Genotype, no chars |
|---|---|---|---|---|---|
| one question per request, own record | 60 | 60 | 40.8k / 36.7k | 1.00 / 0.98 | 0.93 |
| one request per sample, own record | 15 | 60 | 14.7k / 13.7k | 1.00 / 0.97 | 0.87 |
| one joint question per sample (8 combinations) | 15 | 15 | 11.1k / 10.1k | 1.00 / 1.00 | - |
| one request, whole study as state | 1 | 60 | 10.6k / 9.5k | 1.00 / 1.00 | 1.00 |
| whole study, one question per request | 60 | 60 | - / 138.5k | - / 1.00 | 1.00 |

Packing questions into one request changed no answer: 60/60 same, largest probability shift 0.04.
The own record failed on genotype because "Luc KD" reads like a knockdown; only the study summary
says luciferase is the control. In the one-request-per-sample run, the two wrong labels had top
probability 0.63 and 0.69.

**4. Numbers.** The dev study's own numbers were easy: time-span pick among regex spans 15/15
(including three 0 h samples whose protocol says "48 hours"), "more than 24 h" 15/15, hours to days
15/15, later-than pairs 72/72, counts of donors, samples and HIV-1 samples right in both option
orders (the HIV-1 count fell from p 0.98 to 0.77 when reversed), HEPES dose pick and comparison right.
Mixed units, added once those passed: later-than 53/54 (said 2d is not later than 36h, p 0.10;
mean probability of the truth 0.91); ages P7, E14.5, 8 weeks 6/6; "latest of 5" right but with 0.14
to 0.30 on 36h. Code parsing gets all of these right.

**5. Grouping.**

| Task | Pairwise yes/no (105 pairs) | One Choice per sample (15) |
|---|---|---|
| same donor, with chars | 105/105, 0 transitivity breaks | 15/15 among listed donors |
| same donor, no chars | **54/105, 47 false yes, 218 transitivity breaks** | 15/15 when the donor list is in the state |
| same biosample, with / no chars | 105/105 / 105/105 | **0/15 / 0/15**: always picked a sibling sample, never "none" |

## Verdict

- **Jev checks well (measured, one easy study):** a sample's level when the list of levels is
  given, the baseline, factor versus donor versus attribute, signal versus control on RNA-seq, an
  ontology term among code-found candidates, and same-or-not yes/no questions with both records in
  view. The wrong labels I inspected, and the answers that moved with option order, all had top
  probability under 0.72; I did not check every wrong label in every variant.
- **Jev checks badly (measured):** anything that needs a fact from outside the state. "M27" is a
  donor only if the state says so: pairwise donor questions fell to coin-flip. A "none" option in a
  Choice is not picked (0/15). Mixed-unit numbers slip (1/54) and lose confidence; code should
  parse every number, date and count (read: TypeSafe's own limits page says the same).
- **How a claim splits (inferred from the above):** one Choice per sample per factor, all of one
  sample's questions in one request (packing is free and changes nothing), state = the sample's own
  record **plus a short study glossary**: each level with the writer's quote that defines it
  ("control = shRNA targeting luciferase"). That is what whole-study context added. Biosample and
  donor: code blocks candidates, Jev answers yes/no inside a block, or a Choice among listed groups
  paired with a separate yes/no for "does any fit". Re-ask in reversed order only below p 0.9.
- **Cost (estimate.py: measured tokens, inferred counts):** 100 samples ≈ 880 questions, 210
  requests, 0.22M tokens, **$0.009** Jev (+ $0.005 DeepSeek when needed), under 1 minute.
  1,000 samples ≈ 8,700 questions, 2,100 requests, 2.2M tokens, **$0.09** Jev (+ $0.045 DeepSeek),
  about 3 minutes at 8 requests in parallel (rate limit not reached here). All-pairs biosample
  questions would cost 499,500 questions and $16 at 1,000 samples, so code blocking is required.
  Whole-study state stops fitting Jev's 32k per-question limit near 60–70 samples (inferred from
  about 470 state tokens per sample).
- **Fixed sequence or agent (inferred):** a fixed sequence was enough here: two rounds (study, then
  samples), with speculative questions removing the dependency between them, plus one rule-driven
  round that re-asks low-probability labels with more context. Nothing needed an agent's judgement
  about what to ask next. The open lists still need a writing model (one call). This is one
  perturbation study; the ChIP-with-inputs and co-measured-assay studies are the real test of
  signal versus control and of biosample sharing, and were not run.
