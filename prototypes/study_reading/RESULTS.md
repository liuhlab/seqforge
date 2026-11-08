# PROTOTYPE, throwaway: DeepSeek lists + Jev answers on three real GEO studies, against hand readings

DeepSeek (`deepseek-chat`) writes the open lists once per study: factors with levels and quotes, targets,
controls, donors, biosample groups, attributes. Jev (`jev-1.14.0`) answers every closed question those lists
create: each sample's level, donor, assay, target, role, control type, and same-material yes/no for candidate
pairs that code picks. Code derives the five relations. Each claim is marked **measured** (run here),
**read** (from a document) or **inferred**.

Studies: GSE164798 (5xFAD mice by wheel running, 24 RNA-seq), GSE190102 (ChIP-seq of 3 histone marks
with total input, 4 ages, 43 samples), GSE206603 (SuperSeries with RNA-seq and ATAC-seq, 4 iPSC lines,
24 samples). Hand readings: `gold/`. Model readings: `runs/<GSE>/reading.*.yaml`. Scores:
`harness/compare.py`, written to `runs/<GSE>/compare.<version>.json`.

## Versions

| Version | What changed | Gold seen? |
|---|---|---|
| v0 | the harness as handed over: code drafts lists from characteristics that vary (`code-lists`), or DeepSeek writes them (`deepseek-lists`) | no |
| v1 | the HANDOFF's missing parts: target question, control list, donor list with a per-sample donor question, assay-blind biosample blocking (same donor and same factor levels), a study glossary (each listed value with its quote) in every sample's state, relations scoped by assay, target and role, SuperSeries traversal (already worked) | no (gold README only) |
| v2 | donor ids without quotes in the glossary; series also for control libraries; writer told that a cell line is a donor and to split packed fields; paper abstract and methods (NCBI PMC) given to the writer; a study question "one individual per sample?" when no donor is listed; a sample-type question; better ontology candidates | yes |
| v3 | v2 plus the dev study's rule, set before any gold existed: re-ask every pick under p 0.9 with options reversed | yes |

v1 is the honest "run before you look" result. v2 fixes are tuned on these three studies, so its numbers
are an upper bound for unseen studies (inferred). The v1 writer prompt was written after reading the
gold README, which names the traps (read); the YAML was opened only after v1 was frozen.

## Accuracy per study (measured)

Cells are right/total over settled labels; gold labels under `unsure` are counted apart. Relations are
precision P and recall R of exact groups or edges. Factor levels are matched by meaning (`wild-type` = `WT`);
`compare.py` prints each mapping.

### GSE164798 (2 x 3 crossed perturbation)

| Field | v0 code lists | v0 DeepSeek lists | v1 | v2 |
|---|---|---|---|---|
| factors right (levels, baseline, ordered) | 2/2, 2 extra | 2/2 | 2/2 | 2/2 |
| sample factor values | 48/48 | 48/48 | 48/48 | 48/48 |
| donor grouping | 0/24 | 0/24 | 0/24 | 24/24 |
| biosample grouping | 24/24 | 24/24 | 24/24 | 24/24 |
| assay, target, role, control type | 96/96 | 96/96 | 96/96 | 96/96 |
| attributes, text / ontology id | 96/144, 24/48 | 120/144, 24/48 | 120/144, 24/48 | 144/144, 48/48 |
| contrasts | R 0 (0 of 7) | P 1 R 1 | P 1 R 1 | P 1 R 1 |
| replicates | P 1 R 1 | P 1 R 1 | P 1 R 1 | P 1 R 1 |

### GSE190102 (ChIP-seq with input)

| Field | v0 code lists | v0 DeepSeek lists | v1 | v2 |
|---|---|---|---|---|
| factors right | 0/1 (not ordered), +antibody | 0/1, +antibody | 1/1 | 1/1 |
| sample age | 39/43 | 42/43 | 43/43 | 43/43 |
| donor grouping | 0/42 | 0/42 | 42/42 | 41/42 |
| biosample grouping | 1/28 | 1/28 | 28/28 (15 unsure agree) | 28/28 (15 unsure agree) |
| target | 4/43 | 4/43 | 42/43 | 43/43 |
| role, control type | 86/86 | 86/86 | 84/86 | 86/86 |
| attributes, text / ontology id | 101/301, 58/58 | 144/301, 58/101 | 215/301, 58/129 | 230/301, 144/172 |
| contrasts | P 0.26 R 0.75 | P 0.26 R 0.75 | P 0.88 R 0.88 | P 1 R 1 |
| replicates | P 0.75 R 0.82 | P 0.91 R 0.91 | P 0.83 R 0.91 | P 1 R 1 |
| series | R 0 (0 of 4) | R 0 | P 0.67 R 0.5 | P 1 R 1 |
| co-measurement groups | P 0 R 0 | P 0 R 0 | P 1 R 1 (14) | P 1 R 1 |
| background control | R 0 (0 of 39) | R 0 | P 0.81 R 0.97 | P 1 R 1 |
| donor pairs | R 0 | P 0.04 R 1 (903 claimed) | P 1 R 1 | P 1 R 1 |

The 15 samples of 2024 carry age and mark only in the title. Errors among them: v0 code lists 4 ages,
15 targets, 15 donors; v1 one role and one target (the same sample); v2 none.

### GSE206603 (SuperSeries, RNA-seq + ATAC-seq)

| Field | v0 code lists | v0 DeepSeek lists | v1 | v2 |
|---|---|---|---|---|
| study boundary (3 Series, SuperSeries) | right | right | right | right |
| factors right | 1/1, 2 extra | 1/1 | 1/1 | 1/1 |
| sample genotype | 24/24 | 24/24 | 24/24 | 24/24 |
| donor grouping | 0/24 | 0/24 | 0/24 | 24/24 |
| biosample (all 24 unsure in gold) | 4 agree, 20 differ | 4 agree, 20 differ | 24 agree | 24 agree |
| co-measurement claimed | 10 RNA/ATAC pairs | 10 pairs | 0 | 0 |
| assay, target, role, control type | 96/96 | 96/96 | 96/96 | 96/96 |
| attributes, text / ontology id | 48/144, 24/48 | 72/144, 24/72 | 72/144, 24/72 | 144/144, 72/96 |
| contrasts | R 0 (0 of 6) | P 0 R 0 (3 mixed assays) | P 1 R 1 | P 1 R 1 |
| replicates | P 0 R 0 | P 0 R 0 | P 1 R 1 | P 1 R 1 |

Study boundary, BioProject and PMID were right in every version of every study.

## Does Jev's probability track correctness? (measured)

Jev's probability of its own pick, against right or wrong, settled gold labels only. One answer per study
(baseline, factor role, sample type) counts once.

| p of the pick | v0 DeepSeek lists | v1 | v2 |
|---|---|---|---|
| below 0.7 | 1/7 right | 3/5 | 3/3 |
| 0.7 to 0.9 | 4/13 | 28/28 | 21/22 |
| 0.9 to 0.99 | 31/32 | 87/88 | 117/118 |
| 0.99 and above | 299/326 | 362/362 | 352/352 |

- **With right lists, yes.** In v1 and v2, every wrong answer to a question that decided a label had p
  under 0.9 (0.55, 0.56, 0.76). The two wrong answers in the 0.9 to 0.99 band are a speculative control
  type (0.98), used only because the role was wrong, and v2's sample type (0.96), where the gold follows
  a convention. Nothing at 0.99 or above was wrong (714 labels).
- **With a wrong list, no.** v0's writer listed one donor, `C57/BL6` (the strain). Jev put all 43
  samples on it, 27 of them at p 1.0. A Choice can only rank the options it is given. Its probability
  measures the fit among the listed options, not whether the list is right.
- **Mid-range p matched the gold's own doubt.** GSE190102 same-material pairs the gold settled: p_yes
  0.83 to 0.91. Pairs with a 2024 H3K27me3 library, which the gold marks unsure: 0.49 to 0.67. GSE206603
  RNA/ATAC pairs with the same replicate number (gold unsure, no source says they share a
  differentiation): 0.24 to 0.41, the highest of its 60 pairs, all under 0.5 (other pairs at most 0.30).
- **Reversed option order changed nothing.** v3 re-asked 64 picks under p 0.9 with options reversed:
  0 answers changed. 48 of the 64 were speculative control-type questions on signal libraries, never used.

## Errors and their causes

Cause classes: **list** (wrong or missing in DeepSeek's lists), **Jev** (wrong answer to a closed question),
**question** (no question asked, or the wording set the default), **rule** (a derivation rule in code),
**source** (the fact is outside the fetched text), **candidates** (code's ontology candidates).

| Version | Error | Cause | Evidence |
|---|---|---|---|
| v0 | Every varying characteristic became a candidate factor; Jev called `strain`, `treatment synonym`, `disease state` and `treatment` designed factors, so 0 contrasts in GSE164798 and GSE206603 | list (code), Jev | measured. Jev's role question sees one key at a time and cannot tell that two keys restate one factor (inferred) |
| v0 | Antibody listed as a factor; target always null; 23 contrasts across marks (P 0.26) | list, question | measured |
| v0 | Donor list = the strain; 43 samples on one donor at p 1.0 | list | measured |
| v0 | 10 RNA/ATAC pairs with the same replicate number called one biosample (p_yes 0.49 to 0.63): the overreach the gold warns about | question (wording had no default) | measured. With v1's wording, "answer no unless a text says they share a preparation", the same pairs fell to 0.24 to 0.34 |
| v0 | Age not ordered: "3 months" not parsed | rule | measured |
| v1 | GSE190102 GSM8514843 (`155688 WT 12M H3K27me3`) called a control at p 0.56, target none at p 0.55. One wrong label cascaded into 8 extra background-control edges, a split replicate group, a broken series and a broken contrast | Jev, set up by the state: the writer's quote for donor 155688 was another sample's title, `155688 WT 12M TI` | measured. The same question without donor quotes: signal at p 1.0 (`probe_glossary.py`) |
| v1 | Input-library series missing (gold scopes series by role) | rule | measured. My rule was signal-only, a guess before seeing the gold |
| v1 | No donors in GSE164798 (one mouse per sample) or GSE206603 (one iPSC line per genotype) | list | measured. GEO never names the mice; the gold used "rep N" and the paper (read) |
| v1 | Sex missing (GSE190102: only in the paper; GSE206603: packed in `treatment: M, APOE2/3`); APOE genotype missing; sample type never asked | source, list, question | measured |
| v1 | Ontology ids missing: "neurons" (OLS returned only neuron subtypes), forebrain (writer attributes were not looked up), Alzheimer's disease (MONDO search returned numbered subtypes) | candidates | measured. Jev correctly picked "none of these" each time |
| v2 | GSE190102 GSM5825066 (`17 WT 6M H3K27ac`): donor "not stated" at p 0.76 | Jev, set up by the v2 fix that removed donor quotes | measured. With donor quotes, `17` at p 0.98. The quote helps the donor question and hurts the role question |
| v2 | Sample type of sorted nuclei: "primary cells" (p 0.96), gold "tissue" | convention | the gold README calls its choice a convention (read) |
| v2 | Genotype attribute `WT` missing on the 28 samples of 2021, which never state it | list | measured |
| v2 | "NeuN+ neuronal nuclei" mapped to CL:0020009 (prototypic neuron), gold CL:0000540 (neuron); disease ids missing | candidates | measured |

## What Jev checks well and badly on real studies

**Well (measured, three studies plus the dev study):**

- A sample's level from a given list, also from the title alone: GSE190102's 15 title-only samples, age
  and target 15/15 in v2.
- Assay (91/91 at p 0.99 or above), signal versus control, input as the control type, and the target
  among listed marks.
- Baseline and factor-versus-other for a single listed factor (p 0.96 to 1.0, all right).
- Same material, yes or no, for two records with a glossary. Settled pairs were 49/49 right in each of
  v1 and v2, and p stayed mid-range where the sources are silent.
- Choosing "none of these" among ontology candidates when the right term is missing: 3 of 4 cases. The
  fourth picked "prototypic neuron" for "NeuN+ neuronal nuclei".

**Badly (measured):**

- Anything the list gets wrong. Jev cannot reject the options it is given, and its probability stays high.
- Telling two candidate keys apart when they restate one factor (v0 code lists).
- Ignoring irrelevant state. One quote in the glossary flipped a role from 1.0 signal to 0.57 control.
- Recognising an unusual id (`17`) as a donor without an example of it.

## Fixed sequence or agent

**A fixed sequence was enough to produce every reading (measured).** Three rounds: one writer call, one
study request, then per-sample and per-pair requests at 8 in parallel. No step needed to choose what to
ask next. Every v1 and v2 error is a list gap, a state choice, a missing question or a code rule, and a
fixed rule can address each. Three of those rules:

- **Check each list item:** give donors and targets the same role check factors get ("is `C57/BL6` an
  individual animal?").
- **Use a different state per question:** donor quotes go only to the donor question.
- **Mark mid-range answers unsure:** p_yes between about 0.35 and 0.7 on same-material pairs.

The first two are inferred, not run. The third is inferred from the measured pair probabilities.

**An agent would have helped once (inferred):** noticing that a list is wrong from the shape of the
answers. Examples: one donor covering every sample (v0), a factor that duplicates another, or no donor for
cell-line data (v1). Cheap fixed triggers could stand in: one donor for every sample, two factors with
identical sample partitions, more than half the samples "not stated". Each would send the writer back with
the finding. This was not run.

## Cost and time per study (measured, v2 path)

| Study | Samples | DeepSeek: calls, tokens in/out, $, s | Jev: requests (questions), tokens in, $, wall s at 8 parallel | Hand reading (gold README) |
|---|---|---|---|---|
| GSE164798 | 24 | 1, 5,193/375, $0.0020, 2.3 s | 61 (187), 95,485, $0.0040, 2.6 s | about 2 min |
| GSE190102 | 43 | 1, 8,455/684, $0.0032, 3.2 s | 79 (300), 169,412, $0.0071, 3.3 s | about 4 min |
| GSE206603 | 24 | 1, 7,919/564, $0.0029, 3.3 s | 85 (258), 187,111, $0.0079, 3.5 s | about 4 min |

- A reading costs 0.6 to 1.1 cents and 5 to 7 seconds of model time, plus about 1 to 2 s to fetch the
  paper. The first GEO fetch was not timed.
- The hand times are an agent session's wall clock with scripted tabulation, not a person's (read).
- v1 cost the same within $0.002. v3's re-ask added 46 and 18 requests to GSE190102 and GSE206603
  (+$0.0024 and +$0.0008) and fixed nothing.
- This unit spent $0.076 in total: DeepSeek $0.020, Jev $0.057, all versions and probes included. With
  the dev study, all runs together cost $0.116.

## Estimate for 100 and 1,000 samples (`harness/cost.py`; measured means, inferred counts)

Measured means, v2, three studies: 2,068 Jev tokens per sample request, 1,893 per pair request, 1.44
candidate pairs per sample, 112 study-request tokens per sample line, 88 writer tokens per sample line,
0.041 s of wall time per Jev request at 8 in parallel.

| N | Jev requests | Jev tokens | Jev $ | DeepSeek calls, $ | Wall time |
|---|---|---|---|---|---|
| 100 | 245 | 0.49 M | $0.021 | 1, $0.005 | about 10 s Jev + 4 s DeepSeek |
| 1,000 | 2,441 | 4.8 M | $0.20 | 8 (7 chunks of 150 lines + a merge), $0.046 | about 1.7 min Jev + about 8 s DeepSeek |

The dev study's estimate was $0.009 and $0.09 Jev. This one is about 2.2 times higher: real studies need
more questions per sample (donor, target, attributes), a glossary in every state, and more candidate pairs.
Inferred limits at 1,000 samples:

- **Donor Choice:** it has one option per donor. Code should first keep only the ids that appear in the
  sample's own text. The token count above assumes a short list.
- **Study request:** it passes Jev's 32k limit near 280 samples. Above that, send a table of distinct values.
- **Writer:** one call takes about 150 sample lines, so it must be chunked. A donor list of 1,000 ids
  would exceed the 8,000-token output cap.
- **Rate limit:** never reached here. Up to 8 requests ran in parallel.

## What this changes in the model split (inferred from the above)

1. **The open lists are the accuracy bottleneck, not the closed answers.** Jev was right on 714 of 714
   labels at p 0.99 or above in v1 and v2. Every structural error traced to a list item that was wrong,
   missing or unchecked. A writer error passes through Jev untouched, often at high probability. Spend
   effort on checking the lists: a Jev role check per list item, and the triggers above. A stronger
   writer is not needed.
2. **Code can take two of Jev's jobs.** Donor ids are literal tokens in titles (GSE190102). A code match,
   with Jev only as fallback, avoids the quote-in-state problem that caused one error in each version.
3. **The question's default decides relations no source states.** The same RNA/ATAC pairs went from
   10 claimed to 0 claimed when the yes/no question said "no unless a text says so". Keep that wording,
   and record mid-range p as unsure, not as no.
4. **The paper belongs in the writer's input.** It added 2k to 4k tokens and at most $0.0014 per study
   (measured), and supplied sex for GSE190102 (measured). Jev's per-sample state can stay GEO-only.

Caveats: three studies, all under 50 samples, all with clean titles. v2's fixes were made with the
gold open, and v3's rule was set in advance. The GSE206603 margin is thin: the highest same-number pair
reached p_yes 0.41 against a 0.5 cut.
