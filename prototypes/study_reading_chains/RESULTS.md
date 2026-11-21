# Prototype: the model calls that read a study

**Throwaway** (issue #629, map #626). This folder answers one question: *what sequence of model
calls reads a study into the reading format?* It ran five variants of the chain on the 50 studies
of the standard set, from each study's existing `bounds.yaml`, against the toy store, on
2026-11-21 (a Saturday, so DeepSeek's off-peak price applied). Nothing here is imported by
`src/seqforge`. It gives counts and facts for the review to react to. It decides nothing, and it
has **no accuracy number**: the evaluation effort scores readings, not this prototype.

Evidence classes: **measured** means counted from this run's files by `summarize.py`;
**inferred** means reasoned from those counts. "Differs from the example reading" is not "wrong":
the five examples were written and checked by models, and no person has checked them.

## Answer in brief

All numbers are measured from this run's files unless marked inferred. "Chain" names below:
**code lists** (code drafts the lists, no writing model); **Flash** (DeepSeek Flash writes the
lists, thinking off); **Flash thinking** (thinking on); each Flash chain read either records +
abstracts or records + full text. Jev answered every closed question in all five.

| Chain | Studies | Settled | Unsettled | Refused | Spend, all studies | Per study, median | Whole run, wall |
|---|---|---|---|---|---|---|---|
| code lists | 50 | 35 | 14 | 1 | $0.18 (Jev only) | $0.003 | 3.0 min (17 studies from the cache of an interrupted first run) |
| Flash, abstracts | 50 | 33 | 14 | 3 | $0.27 | $0.005 | 3.5 min |
| Flash, full text | 42 | 31 | 8 | 3 | $0.30 | $0.007 | 3.5 min |
| Flash thinking, abstracts | 50 | 39 | 11 | 0 | $0.59 | $0.011 | 9.6 min |
| Flash thinking, full text | 42 | 33 | 9 | 0 | $0.66 | $0.015 | 9.6 min |

The full-text chains ran on the 42 studies whose paper the toy store holds as article XML (38)
or PDF (4); 8 studies have no held paper file. No study of the 50 has a microarray sample, so
the sequenced-only filter dropped nothing.

- **Refusals come from the writer's JSON, not from the format.** Flash with thinking off wrote
  invalid JSON for 3 of 50 studies (abstracts) and 3 of 42 (full text), such as an unescaped
  quote mark inside a quote. Those 6 studies end refused; no retry was tried. With thinking on,
  0 of 92. The code lists were refused once (GSE182981): a factor level no sample was put at.
  No assembled file failed the format check for any other reason.
- **The code lists miss what is not in a typed field.** They wrote no factor for 2 studies
  (GSE100021, whose treatment is only in `Sample_description`; GSE288106), made 4 factors from
  the source name, and left 3,684 of 12,592 sample-label claims `unknown` against 2,082 with
  Flash thinking on the same studies. Every writer chain wrote at least one factor for every
  study it did not refuse.
- **Thinking changes the lists.** 16% of sample-label values differ between Flash and Flash
  thinking on abstracts, and their factor labels differ on 21 of 47 studies. Thinking costs about
  twice as much and takes about 9 times longer per writer call (57 s against 6 s, median;
  163 s at most).
- **Full text fills labels the abstract leaves `unknown`.** With thinking on, reading the full
  text gave a value where the abstract chain had `unknown` for 189 sample claims on sex, 145 on
  strain, 132 on development stage, 101 on age and 78 on disease, and lost a value in at most 13
  per label. It added $0.004 and 22 s per study (median).
- **Quotes come back; most are judged to back their claim.** Code found 99.6 to 100% of each
  chain's quotes exactly (1 to 3 not found per writer chain); Jev judged 94 to 98% of the found
  ones to back their claim. Quotes from a paper's full text backed less often: 42 of 52 (Flash)
  and 103 of 115 (Flash thinking).
- **Ontology candidates are missing more often than they are wrong.** 51 to 59% of label texts
  got no candidate at all from OLS4 or Cellosaurus, in 38 (code lists) to 46 (Flash thinking) of
  the 50 studies, because the text is the source's own words ("Rat hippocampus", "Alzheimer's disease (5XFAD)", "Primary
  Microglia"). With an exact hit among the candidates Jev picked a term 86% of the time; with
  only plain hits it picked "none" about 65% of the time. **Jev does pick "none"**: asked again
  with its first pick removed, it chose "none" 181 of 263 times (69%); the other 82 picks were
  mostly a near-synonym or a parent term (listed below).
- **Asking twice flags factors, not samples.** Reversing the options changed 0.15 to 0.8% of
  the per-sample picks. The study-level asks contradicted each other more: the plain and negated
  "varied on purpose?" disagreed on 3 to 13 factors per chain, and the baseline changed with
  reversed options on 5 to 12. Those two make most of the 8 to 14 unsettled studies per chain.
- **Jev keeps almost every factor the writer lists**: 92 of 94, 75 of 75, 97 of 97 and 81 of 81
  in the writer chains; 114 of 127 of code's candidates.
- **The pull-down and its input were never tied.** Code asked Jev 880 same-material pair
  questions over all chains, with the wording "answer no unless a text says so"; Jev said yes to
  11. On GSE235532, where the example reading ties each input to two pull-downs by the replicate
  number in the title, its highest answer was 0.47 of 40 pairs.
- **Spend for the whole prototype:** DeepSeek **$1.10**, Jev **$0.90** (every logged call,
  pilots and superseded requests included; `spend.py`). The cap was $25 per model.

## The chain as run

One study at a time, two studies in flight, at most 8 model requests in flight. Every step's
reply is cached in `_logs/` (git-ignored), so a rerun costs nothing.

| Step | Who | Input | Output |
|---|---|---|---|
| 0 | **code** (chain "code lists") | the sequenced samples' typed fields: `Sample_characteristics_ch1` as `key: value`, `Sample_source_name_ch1`, `Sample_organism_ch1`, `Sample_library_strategy` | the open lists: each characteristics key's distinct values, with the line itself as the quote; a key that varies over every sample is a candidate factor. **Jev** maps each key to one of the labels (a closed question) |
| 0 | **DeepSeek Flash** (`deepseek-flash`, thinking off or on) | the records (every line `FIELD: value`) and each paper's title and abstract, or its full text | the open lists in JSON: factors with levels, a baseline and a quote; for each of the 16 value labels the distinct values with a quote, or a gap (`na` or `unknown`) with a reason; study-own labels; donors; shared material; the format gaps it sees |
| A | Jev, one request per study | records and abstracts | for each listed factor: varied on purpose? (asked plain **and** negated), its baseline (options in order **and** reversed), ordered? For a label with no value: `na` or `unknown`. The donor kind. 16 yes/no probes for the format's known gaps |
| B | Jev, two requests per sample | the sample's record, the Series title and design, the lists with their quotes | the sample's level on each kept factor and its value on each label, from the listed options plus `unknown` and `na`. Asked twice: options in order, then reversed |
| C | code, then Jev | every quote in the lists | code finds the quote in its record field (character for character) or its paper (after collapsing white space). Jev, one request per study: does each found passage back its claim? And for candidate pull-down/control pairs: were they made from the same material? |
| D | OLS4 and Cellosaurus, then Jev | each distinct text picked for tissue, cell type, cell line, disease, treatment, stage | up to 8 candidates (OLS4 `exact=true` hits first, then plain hits; Cellosaurus for cell lines). Jev picks one or "none". Then the **none probe**: the same question again with Jev's pick removed |
| E | code | all of the above | the reading file (format 1), the format check, the ending |

**How a claim gets its `p`.** A sample's claim: Jev's probability of the option it picked. A
claim under `shared` (the same claim on every sample): the smallest of those probabilities. A
factor: Jev's p(varied on purpose). An `na` or `unknown` with no listed value: Jev's probability of
that answer. `biosample` and a donor id code minted: `p: null` (code's own call), except a
biosample tie Jev answered, which takes its p.

**How a claim gets its backing.** `quoted` when code finds the quote exactly and Jev gives the
passage-backs-claim question p >= 0.5. Found but not backing: `inferred` with `see`. Not found:
`inferred`. A term is written when Jev's pick has p >= 0.8 (species from `Sample_taxid_ch1`, sex
from the four words).

**How a reading ends.**

- **Refused:** the writer's answer is not JSON, or the assembled file fails `check_reading`. The
  file is not written; `out/<variant>/<ID>.refused.yaml` holds the problems.
- **Unsettled (as run):** the two asks disagree on whether a factor was varied on purpose, on its
  baseline, or on any sample's level of a kept factor. The file is written; the result says why.
- **Settled:** otherwise.

Other unsettled rules were computed over the same files for comparison (below).

## Where the chains differ from the five example readings

The comparison is by text, loosely (case and punctuation ignored), so a wording difference
("Rat hippocampus" against "hippocampus") counts as a difference. The full tables are below.

- **Factors.** Every writer chain found the example's factors on all five studies, with level
  names of its own (GSE112068 has no paper file, so only the abstract chains ran there). The
  code lists found them on four; on GSE100021 they found none, and on GSE117724 they added
  `strain` and `source_name` beside the example's two.
- **The pulled-down mark as a factor.** On GSE235532 every chain made `target` a factor; the
  example says the mark is not a factor because it has no baseline. GSE147401 is the same.
- **GSE235532, Flash thinking, full text** differs from the example on 0 of 384 sample-label
  texts; it still has 24 biosample ids where the example has 8.
- **Recurring differences** are on development stage (the writer quotes "28 months old" or a
  stage word where the example infers "adult"), disease (`healthy` written or left `na`), and
  the dose of GSE100021: Flash with full text wrote the value 1.7e12 viral particles from the
  same PDF sentence the example's checker disputed.

## Format gaps over the 50

Three sources count them, and they do not agree. **Code** counts from the records, the paper
files and the written readings. **Jev** answered 16 yes/no probes per study over the records and
abstracts. **The writer** listed the gaps it saw (one of 22 kinds). The tables are below.
Highlights, against the list of issue #603 (studies of 50):

| Gap (issue #603) | Code | Jev probe (p >= 0.5) | Writer, Flash thinking, abstracts |
|---|---|---|---|
| Sources that disagree | not countable by code | 16 | 18 |
| One `p` for two beliefs: a level and an exact value | 19 readings | | 0 |
| One `p` for two beliefs: shared material and control tie | 2 readings | | |
| Treatment: untreated or vehicle group | | 37 | 7 |
| Treatment: two compounds on one sample | | 6 | 4 |
| Treatment: route or side | | 12 | 0 |
| Treatment: dose per side or per animal | | 6 | 4 |
| Treatment: duration with no number | | 7 | 8 |
| Treatment: what a time is measured from | | 18 | 9 |
| Text said once (the same note on 2+ rows) | 50 readings | | |
| A group smaller than the study | 38 readings | | 7 |
| Series membership (samples in 2+ Series) | 7 | | 6 |
| A factor over part of the study | 11 readings | | 24 |
| Factors crossed but not fully | 19 readings | | 22 |
| Pooled material | 21 readings (Jev's donor answer "pool") | 27 | 19 |
| Held constant | | 50 | 7 |
| Batch | 2 (a characteristics key) | 9 | 12 |
| Sequencer (2+ instrument models) | 6 | 7 | 6 |
| Term source missing: assay | 50 | | 26 (any term) |
| Term source missing: target | 5 (ChIP studies) | | |
| Term source missing: vector | | 5 | |
| Term source missing: rat stage | 2 | | |
| Quotes from a PDF | 4 studies with a PDF full text | | |
| One control for pull-downs of different material | | 1 | 3 |
| A comparison group used as the control, no input | | 1 | 15 |
| The archive's BioSample per library against a shared biosample | | | 1 |

New gaps this run hit:

- **Label text cannot be looked up.** The format writes `text` in the source's words, and those
  words found no ontology candidate for 51 to 59% of texts (38 to 46 studies). A short search
  form of the text, written beside `text` or made by code, is missing.
- **Libraries that are not samples.** GSE329430 deposits hashtag, CMO and antibody-tag (ADT)
  libraries as GEO samples. The writers named them as assays ("HTO", "CMO", "ADT"); the format
  has no way to say a library is a feature-barcode library of another sample. 1 study.
- **"Healthy" has no MONDO term.** The format says `PATO:0000461`; the prototype searched MONDO
  only, so `healthy` got no candidate. A code mapping, not a format change.
- **Mouse stages.** MmusDv gave candidates for 55 of 63 texts, and Jev picked "none" for 44;
  the text was the word "adult" in 43 of the 44.

## The assays the general instructions fail

One set of writer instructions served every assay. By assay (settled/unsettled/refused per
chain in the table below), nothing is refused for an assay reason. What goes wrong:

- **Pull-down (ChIP-Seq, 5 studies).** The input is never tied to its pull-down (above). Two
  studies get the mark as a factor. Where the target is in no typed field (GSE125850 names its
  inputs in the title; GSE129928's titles name no mark), the code lists leave target `unknown`;
  the writer chains find it.
- **Libraries GEO calls OTHER (3 studies).** Ribosome profiling (GSE131269, GSE97461) gets a
  different assay text in every chain ("RiboSeq", "Ribosomal profiling", "ribosome
  footprinting", "OTHER"). Single-cell with hashing (GSE329430) is the new gap above.
- **Single cell (16 studies by Jev's probe).** The reading describes libraries, not cells, as
  the map decides; nothing failed on it in this run.
- **Bisulfite-Seq, ATAC-seq, miRNA-Seq, ncRNA-Seq:** no assay-specific failure seen.

## The decisions the review must make

Each item gives the evidence; none is decided here.

1. **Which chain writes the lists.**
   - *Code lists:* the cheapest ($0.003 a study), never invalid, and its quotes are found 100%.
     But it cannot read titles or descriptions: no factor on 2 studies, target `unknown` on 2
     ChIP studies, 4 factors made of the source name, and 1.8 times as many `unknown` claims.
   - *Flash, thinking off:* $0.005 to $0.007 a study, 6 s a call, but 6 of 92 answers were
     invalid JSON and those studies end refused unless a retry is added (not tried).
   - *Flash, thinking on:* $0.011 to $0.015 a study, about a minute a call (163 s at most), no
     invalid answer in 92, and its lists differ from thinking off on 16% of sample values.
   - Not tried: gpt-6-luna (no OpenAI credit), a retry on invalid JSON, a code-lists-then-writer
     hybrid.
2. **What is read.** Full text turns many `unknown` claims into values (sex, strain, stage, age,
   disease, above) for about $0.004 and 22 s more a study with thinking on. 8 of the 50 have no
   paper file, and 4 have only a PDF, whose quotes lose superscripts. Paper quotes backed their
   claim less often (88 to 90%, thinking) than record quotes (97%).
3. **When a reading ends unsettled.** As run (the two asks disagree on a factor or a level),
   8 to 14 studies of each chain. Over the same files: any per-sample pick that changes with
   reversed options, 5 to 20; any factor or level claim with p < 0.8, 10 to 13; any kept factor
   with p(varied) < 0.9, 8 to 20; any claim with p < 0.5, 6 to 17. Reversing options moved
   almost no per-sample pick, so the asking-twice signal comes from the study-level factor
   questions. Jev almost never rejects a listed factor, so a wrong factor in the list passes with
   a high p unless the ask-twice catches it (inferred from the counts above and the earlier
   trial's finding that a Choice ranks only the options it is given).
4. Smaller calls the run surfaced: whether an invalid writer answer gets one retry; whether a
   factor level no sample is at is dropped by code or refuses the study; whether the
   pull-down/control tie needs a different question (title tokens, or code matching replicate
   numbers); whether the ontology search gets a short query form; whether a mid-range p (0.5 to
   0.8, where most `unknown` and `na` claims sit) means anything to a reader.

## Limits

- No study was held out: the writer instructions and Jev questions were written once, piloted on
  GSE100021, GSE235532 and GSE117724, and not changed after the 50 ran, except one fix: code's
  pull-down/control pair rule first required equal levels on every factor, so no pair was ever
  asked once the mark was a factor. It now ignores factors on target, control type and assay,
  and every variant was re-run from the cached replies with the fix.
- One run per call. The gateway and `_logs/` cache every reply, so re-runs repeat the same
  answers; how much the writer varies between runs was not measured.
- Jev's per-sample state is the sample's record plus the lists, not the paper. A value only the
  paper gives reaches Jev through the writer's quote in the lists.
- The study-level Jev request reads records and abstracts (cut to 90,000 characters), never the
  full text.

## Tables (generated by `summarize.py`)

Per study, per chain: [`per_study.md`](per_study.md).

### Endings, cost and time per chain

| Chain | Studies | Settled | Unsettled | Refused | DeepSeek $ (all) | Jev $ (all) | $ per study, median / max | Writer s, median / max | Jev s summed over requests, median / max |
|---|---|---|---|---|---|---|---|---|---|
| code lists | 50 | 35 | 14 | 1 | $0.000 | $0.176 | $0.0029 / $0.0100 | 0.0 / 0.0 | 10.3 / 26.6 |
| Flash, abstracts | 50 | 33 | 14 | 3 | $0.086 | $0.188 | $0.0051 / $0.0129 | 6.2 / 11.6 | 10.7 / 69.8 |
| Flash, full text | 42 | 31 | 8 | 3 | $0.140 | $0.156 | $0.0068 / $0.0132 | 6.9 / 11.4 | 9.6 / 19.1 |
| Flash thinking, abstracts | 50 | 39 | 11 | 0 | $0.386 | $0.203 | $0.0108 / $0.0237 | 57.2 / 122.2 | 10.8 / 30.5 |
| Flash thinking, full text | 42 | 33 | 9 | 0 | $0.483 | $0.179 | $0.0152 / $0.0267 | 79.0 / 163.2 | 10.7 / 53.7 |

Refusal reasons:

- code lists, GSE182981: the format check rejected the answer; `factors.treatment.levels`: no sample is at the level HGD-1; `factors.treatment.levels`: no sample is at the level HGD-5
- Flash, abstracts, GSE106105: the writer's answer is not a JSON object; 
- Flash, abstracts, GSE281157: the writer's answer is not a JSON object; 
- Flash, abstracts, GSE306935: the writer's answer is not a JSON object; 
- Flash, full text, GSE129928: the writer's answer is not a JSON object; 
- Flash, full text, GSE234962: the writer's answer is not a JSON object; 
- Flash, full text, GSE306935: the writer's answer is not a JSON object; 

| Chain | Writer's answer not JSON | Format check: only a level no sample is at | Format check: other | Crash |
|---|---|---|---|---|
| code lists | 0 | 1 | 0 | 0 |
| Flash, abstracts | 3 | 0 | 0 | 0 |
| Flash, full text | 3 | 0 | 0 | 0 |
| Flash thinking, abstracts | 0 | 0 | 0 | 0 |
| Flash thinking, full text | 0 | 0 | 0 | 0 |

Unsettled reasons (as run: the two asks disagree on a factor or on a sample's level):

- code lists: factor X: plain and negated asks disagree (12); factor X: baseline differs when options are reversed (10); N sample levels differ when options are reversed (1)
- Flash, abstracts: factor X: plain and negated asks disagree (8); factor X: baseline differs when options are reversed (8); N sample levels differ when options are reversed (3)
- Flash, full text: factor X: baseline differs when options are reversed (6); factor X: plain and negated asks disagree (3); N sample levels differ when options are reversed (2)
- Flash thinking, abstracts: factor X: baseline differs when options are reversed (7); factor X: plain and negated asks disagree (6); N sample levels differ when options are reversed (2)
- Flash thinking, full text: factor X: baseline differs when options are reversed (5); N sample levels differ when options are reversed (5); factor X: plain and negated asks disagree (4)

### The same readings under other unsettled rules (count of studies, of those not refused)

| Chain | Not refused | As run: asks disagree on a factor or a level | A per-sample pick on any label changes with options reversed | A factor or level claim with p < 0.8 | A kept factor with p(varied) < 0.9 | Any claim with p < 0.5 |
|---|---|---|---|---|---|---|
| code lists | 49 | 14 | 5 | 10 | 20 | 6 |
| Flash, abstracts | 47 | 14 | 18 | 13 | 12 | 12 |
| Flash, full text | 39 | 8 | 20 | 11 | 8 | 17 |
| Flash thinking, abstracts | 50 | 11 | 13 | 10 | 10 | 15 |
| Flash thinking, full text | 42 | 9 | 16 | 11 | 10 | 12 |

### Asking twice

| Chain | Factors listed | Varied on purpose: plain and negated asks contradict | Baseline differs with options reversed | Per-sample claims | Per-sample picks that change with options reversed | of which a factor level |
|---|---|---|---|---|---|---|
| code lists | 127 | 13 | 12 | 14393 | 22 | 1 |
| Flash, abstracts | 94 | 8 | 8 | 14537 | 99 | 16 |
| Flash, full text | 75 | 3 | 6 | 12168 | 99 | 6 |
| Flash thinking, abstracts | 97 | 6 | 7 | 15402 | 58 | 13 |
| Flash thinking, full text | 81 | 4 | 5 | 12720 | 98 | 18 |

### How the probabilities spread (every claim of every written or refused reading)

| Chain | Backing | Claims | <0.1 | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 | 0.95 | 0.99+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| code lists | quoted | 2289 | 0 | 0 | 0 | 1 | 0 | 16 | 12 | 10 | 24 | 55 | 100 | 2071 |
| code lists | inferred | 450 | 0 | 0 | 0 | 0 | 0 | 25 | 27 | 65 | 51 | 45 | 108 | 129 |
| code lists | unknown | 248 | 0 | 0 | 0 | 1 | 8 | 26 | 30 | 51 | 45 | 21 | 34 | 32 |
| code lists | na | 382 | 0 | 0 | 0 | 1 | 2 | 28 | 18 | 32 | 25 | 48 | 33 | 195 |
| code lists | p: null (code's own call) | 787 |  |  |  |  |  |  |  |  |  |  |  |  |
| Flash, abstracts | quoted | 3327 | 0 | 0 | 2 | 5 | 13 | 67 | 46 | 43 | 92 | 129 | 401 | 2529 |
| Flash, abstracts | inferred | 585 | 0 | 0 | 1 | 1 | 16 | 19 | 28 | 67 | 93 | 63 | 112 | 185 |
| Flash, abstracts | unknown | 274 | 0 | 0 | 1 | 6 | 8 | 54 | 24 | 42 | 53 | 23 | 37 | 26 |
| Flash, abstracts | na | 591 | 0 | 0 | 0 | 3 | 10 | 60 | 40 | 62 | 104 | 76 | 61 | 175 |
| Flash, abstracts | p: null (code's own call) | 750 |  |  |  |  |  |  |  |  |  |  |  |  |
| Flash, full text | quoted | 2763 | 0 | 0 | 0 | 2 | 9 | 50 | 45 | 61 | 129 | 127 | 401 | 1939 |
| Flash, full text | inferred | 573 | 0 | 0 | 0 | 3 | 5 | 14 | 22 | 72 | 25 | 64 | 113 | 255 |
| Flash, full text | unknown | 236 | 0 | 0 | 1 | 4 | 13 | 46 | 37 | 42 | 41 | 18 | 25 | 9 |
| Flash, full text | na | 514 | 0 | 0 | 0 | 3 | 18 | 52 | 26 | 59 | 69 | 78 | 77 | 132 |
| Flash, full text | p: null (code's own call) | 621 |  |  |  |  |  |  |  |  |  |  |  |  |
| Flash thinking, abstracts | quoted | 3412 | 0 | 0 | 0 | 0 | 10 | 39 | 30 | 46 | 85 | 155 | 490 | 2557 |
| Flash thinking, abstracts | inferred | 567 | 0 | 0 | 0 | 2 | 13 | 35 | 8 | 75 | 73 | 88 | 112 | 161 |
| Flash thinking, abstracts | unknown | 263 | 0 | 0 | 0 | 2 | 18 | 29 | 43 | 37 | 40 | 25 | 38 | 31 |
| Flash thinking, abstracts | na | 628 | 0 | 0 | 0 | 1 | 9 | 37 | 60 | 82 | 106 | 99 | 74 | 160 |
| Flash thinking, abstracts | p: null (code's own call) | 782 |  |  |  |  |  |  |  |  |  |  |  |  |
| Flash thinking, full text | quoted | 3339 | 0 | 0 | 0 | 2 | 30 | 60 | 46 | 79 | 169 | 182 | 466 | 2305 |
| Flash thinking, full text | inferred | 560 | 0 | 0 | 0 | 19 | 0 | 16 | 45 | 62 | 70 | 86 | 131 | 131 |
| Flash thinking, full text | unknown | 166 | 0 | 0 | 0 | 0 | 9 | 32 | 29 | 26 | 25 | 14 | 23 | 8 |
| Flash thinking, full text | na | 596 | 0 | 0 | 0 | 2 | 16 | 46 | 37 | 56 | 100 | 109 | 92 | 138 |
| Flash thinking, full text | p: null (code's own call) | 651 |  |  |  |  |  |  |  |  |  |  |  |  |

Jev's p(varied on purpose) for each listed factor:

| Chain | Factors | <0.1 | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | 0.7 | 0.8 | 0.9 | 0.95 | 0.99+ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| code lists | 127 | 1 | 2 | 3 | 4 | 3 | 2 | 8 | 7 | 13 | 15 | 56 | 13 |
| Flash, abstracts | 94 | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 5 | 10 | 10 | 47 | 19 |
| Flash, full text | 75 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 3 | 4 | 10 | 37 | 20 |
| Flash thinking, abstracts | 97 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 9 | 6 | 58 | 22 |
| Flash thinking, full text | 81 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 2 | 6 | 5 | 48 | 16 |

### Quotes: found by code, backed by Jev

| Chain | Quotes given | Found in the named field | Found elsewhere | Only loosely (case, hyphen) | Not found | Found exactly | Backed (Jev p >= 0.5) | By source |
|---|---|---|---|---|---|---|---|---|
| code lists | 611 | 611 | 0 | 0 | 0 | 611 | 596 | record 611 found / 596 backed |
| Flash, abstracts | 856 | 833 | 20 | 0 | 3 | 853 | 815 | paper: abstract 5 found / 4 backed; paper: title 1 found / 1 backed; record 847 found / 810 backed |
| Flash, full text | 689 | 648 | 39 | 0 | 2 | 687 | 649 | paper: abstract 5 found / 5 backed; paper: full text 52 found / 42 backed; paper: title 2 found / 2 backed; record 628 found / 600 backed |
| Flash thinking, abstracts | 922 | 897 | 23 | 1 | 1 | 920 | 893 | loose only (record) 1; paper: abstract 17 found / 15 backed; paper: title 4 found / 4 backed; record 899 found / 874 backed |
| Flash thinking, full text | 847 | 780 | 64 | 0 | 3 | 844 | 813 | paper: abstract 17 found / 16 backed; paper: full text 115 found / 103 backed; paper: title 3 found / 3 backed; record 709 found / 691 backed |

### Ontology picks

| Chain | Label texts asked | No candidates | An exact hit among them | Picked a term | Picked none | Picked a non-exact term over an exact one | Plain hits only | Picked a term | Picked none | Probe: asked again without the pick | Probe: picked none |
|---|---|---|---|---|---|---|---|---|---|---|---|
| code lists | 199 | 108 | 46 | 38 | 8 | 0 | 45 | 14 | 31 | 44 | 34 |
| Flash, abstracts | 265 | 156 | 55 | 47 | 8 | 1 | 54 | 18 | 36 | 56 | 39 |
| Flash, full text | 223 | 129 | 48 | 42 | 6 | 1 | 46 | 17 | 29 | 46 | 31 |
| Flash thinking, abstracts | 272 | 140 | 64 | 56 | 8 | 0 | 68 | 22 | 46 | 64 | 42 |
| Flash thinking, full text | 266 | 151 | 51 | 43 | 8 | 0 | 64 | 24 | 40 | 53 | 35 |

Probe picks that were not none (the pick removed, Jev chose another candidate), thinking + full text chain:

- tissue "Fat body": first UBERON:0003917 (arthropod fat body), then UBERON:0003916 (fat pad) at p 0.59
- tissue "Thorax": first UBERON:0000915 (thoracic segment of trunk), then UBERON:0001443 (chest) at p 0.77
- disease "prion disease": first MONDO:0005429 (prion disease), then MONDO:0700108 (prion disease, non-human animal) at p 0.79
- cell_type "germ cell": first CL:0000586 (germ cell), then CL:0000039 (germ line cell) at p 0.5
- cell_type "muscle stem cell (MuSC)": first CL:0008011 (skeletal muscle satellite stem cell), then CL:0000355 (multi-potent skeletal muscle stem cell) at p 0.92
- tissue "tibialis anterior": first UBERON:0001385 (tibialis anterior), then UBERON:0004499 (skeletal muscle tissue of tibialis anterior) at p 0.96
- tissue "Pectoralis Major": first UBERON:0002381 (pectoralis major), then UBERON:0004510 (skeletal muscle tissue of pectoralis major) at p 0.9
- tissue "Rectus Abdominis": first UBERON:0002382 (rectus abdominis muscle), then UBERON:0004511 (skeletal muscle tissue of rectus abdominis) at p 0.61
- tissue "hindlimb muscle": first UBERON:0003663 (hindlimb muscle), then UBERON:0001383 (muscle of leg) at p 0.58
- development_stage "adult": first WBls:0000041 (adult Ce), then WBls:0000060 (reproductive stage adult hermaphrodite Ce) at p 0.59
- tissue "heart": first UBERON:0000948 (heart), then UBERON:0007100 (primary circulatory organ) at p 0.44
- cell_type "microglia": first CL:0000129 (microglial cell), then CL:0002629 (mature microglial cell) at p 0.54
- treatment "LPS": first CHEBI:16412 (lipopolysaccharide), then CHEBI:89981 (LPS with O-antigen) at p 0.42
- cell_type "Muscle Stem Cells": first CL:0000355 (multi-potent skeletal muscle stem cell), then CL:0008011 (skeletal muscle satellite stem cell) at p 0.87
- disease "ALS/FTD": first MONDO:0007105 (frontotemporal dementia and/or amyotrophic lateral sclerosis 1), then MONDO:0017161 (frontotemporal dementia with motor neuron disease) at p 0.65

By vocabulary, all chains: cellosaurus: 43 asked, 15 term written, 7 none, 16 no candidates; chebi: 334 asked, 36 term written, 75 none, 217 no candidates; cl: 186 asked, 37 term written, 22 none, 113 no candidates; fbdv: 12 asked, 4 term written, 4 none, 0 no candidates; hsapdv: 8 asked, 6 term written, 0 none, 2 no candidates; mmusdv: 63 asked, 1 term written, 44 none, 8 no candidates; mondo: 211 asked, 23 term written, 11 none, 173 no candidates; uberon: 184 asked, 90 term written, 17 none, 52 no candidates; wbbt,uberon: 110 asked, 12 term written, 6 none, 82 no candidates; wbls: 74 asked, 6 term written, 34 none, 21 no candidates

### Where the chains differ from each other

| Chain | Factors written (studies by count) | Sample-label claims unknown | na | of | Study-own labels written |
|---|---|---|---|---|---|
| code lists | 102 ([(2, 18), (1, 14), (3, 9)]) | 3684 | 4272 | 12592 | 1 |
| Flash, abstracts | 92 ([(2, 25), (1, 13), (3, 7)]) | 1964 | 3765 | 12032 | 47 |
| Flash, full text | 75 ([(2, 17), (1, 13), (3, 8)]) | 1411 | 3189 | 10048 | 43 |
| Flash thinking, abstracts | 97 ([(1, 19), (2, 18), (3, 11)]) | 2082 | 3949 | 12592 | 61 |
| Flash thinking, full text | 81 ([(2, 19), (1, 13), (3, 10)]) | 922 | 3195 | 10496 | 51 |

Pairwise: share of sample-label values (16 labels, text compared loosely) that differ, and studies whose factor labels differ:

| Chain A | Chain B | Studies both wrote | Sample-label values that differ | Studies whose factors differ |
|---|---|---|---|---|
| code lists | Flash, abstracts | 47 | 2574/12032 (21%) | 30 |
| code lists | Flash, full text | 39 | 2334/10048 (23%) | 25 |
| code lists | Flash thinking, abstracts | 50 | 2741/12592 (22%) | 32 |
| code lists | Flash thinking, full text | 42 | 3172/10496 (30%) | 26 |
| Flash, abstracts | Flash, full text | 37 | 1618/9680 (17%) | 15 |
| Flash, abstracts | Flash thinking, abstracts | 47 | 1905/12032 (16%) | 21 |
| Flash, abstracts | Flash thinking, full text | 39 | 2168/9936 (22%) | 18 |
| Flash, full text | Flash thinking, abstracts | 39 | 2074/10048 (21%) | 15 |
| Flash, full text | Flash thinking, full text | 39 | 2081/10048 (21%) | 17 |
| Flash thinking, abstracts | Flash thinking, full text | 42 | 2213/10496 (21%) | 10 |

Per label, share of sample values that differ between Flash thinking with full text and the code lists, and between the two Flash chains with full text (thinking off vs on):

| Label | thinking+full vs code | Flash full: thinking off vs on | Flash: abstracts vs full text |
|---|---|---|---|
| assay | 7% | 5% | 5% |
| target | 9% | 6% | 6% |
| control_type | 6% | 0% | 1% |
| species | 0% | 0% | 0% |
| sex | 56% | 25% | 30% |
| age | 36% | 34% | 20% |
| development_stage | 66% | 47% | 47% |
| tissue | 19% | 20% | 11% |
| cell_type | 12% | 13% | 6% |
| cell_line | 6% | 6% | 3% |
| disease | 87% | 47% | 48% |
| genotype | 25% | 15% | 12% |
| strain | 34% | 21% | 22% |
| treatment | 34% | 31% | 26% |
| dose | 44% | 27% | 12% |
| duration | 42% | 34% | 18% |

Per label, how a sample's claim changes from chain A to chain B: unknown in A and a value in B (gained), a value in A and unknown in B (lost), a value in both with different words (reworded or different), na in one and a value in the other:

| A -> B | Label | Sample claims compared | Gained a value | Lost a value | Both valued, words differ | na in one, value in other |
|---|---|---|---|---|---|---|
| Flash, abstracts -> Flash, full text | assay | 605 | 0 | 0 | 33 | 0 |
| Flash, abstracts -> Flash, full text | target | 605 | 0 | 0 | 18 | 18 |
| Flash, abstracts -> Flash, full text | control_type | 605 | 0 | 0 | 0 | 7 |
| Flash, abstracts -> Flash, full text | sex | 605 | 99 | 29 | 45 | 8 |
| Flash, abstracts -> Flash, full text | age | 605 | 63 | 0 | 61 | 0 |
| Flash, abstracts -> Flash, full text | development_stage | 605 | 74 | 80 | 130 | 0 |
| Flash, abstracts -> Flash, full text | tissue | 605 | 5 | 2 | 31 | 12 |
| Flash, abstracts -> Flash, full text | cell_type | 605 | 12 | 0 | 24 | 3 |
| Flash, abstracts -> Flash, full text | cell_line | 605 | 0 | 0 | 0 | 16 |
| Flash, abstracts -> Flash, full text | disease | 605 | 48 | 20 | 142 | 76 |
| Flash, abstracts -> Flash, full text | genotype | 605 | 8 | 3 | 57 | 2 |
| Flash, abstracts -> Flash, full text | strain | 605 | 104 | 20 | 9 | 0 |
| Flash, abstracts -> Flash, full text | treatment | 605 | 0 | 16 | 62 | 73 |
| Flash, abstracts -> Flash, full text | dose | 605 | 21 | 11 | 24 | 2 |
| Flash, abstracts -> Flash, full text | duration | 605 | 27 | 32 | 24 | 3 |
| Flash thinking, abstracts -> Flash thinking, full text | assay | 656 | 0 | 0 | 17 | 0 |
| Flash thinking, abstracts -> Flash thinking, full text | target | 656 | 0 | 0 | 26 | 3 |
| Flash thinking, abstracts -> Flash thinking, full text | control_type | 656 | 0 | 0 | 0 | 1 |
| Flash thinking, abstracts -> Flash thinking, full text | sex | 656 | 189 | 12 | 28 | 10 |
| Flash thinking, abstracts -> Flash thinking, full text | age | 656 | 101 | 2 | 78 | 18 |
| Flash thinking, abstracts -> Flash thinking, full text | development_stage | 656 | 132 | 13 | 102 | 0 |
| Flash thinking, abstracts -> Flash thinking, full text | tissue | 656 | 0 | 0 | 156 | 24 |
| Flash thinking, abstracts -> Flash thinking, full text | cell_type | 656 | 54 | 0 | 21 | 12 |
| Flash thinking, abstracts -> Flash thinking, full text | cell_line | 656 | 0 | 0 | 22 | 12 |
| Flash thinking, abstracts -> Flash thinking, full text | disease | 656 | 78 | 0 | 115 | 152 |
| Flash thinking, abstracts -> Flash thinking, full text | genotype | 656 | 12 | 5 | 90 | 0 |
| Flash thinking, abstracts -> Flash thinking, full text | strain | 656 | 145 | 2 | 54 | 0 |
| Flash thinking, abstracts -> Flash thinking, full text | treatment | 656 | 7 | 0 | 113 | 94 |
| Flash thinking, abstracts -> Flash thinking, full text | dose | 656 | 41 | 0 | 52 | 33 |
| Flash thinking, abstracts -> Flash thinking, full text | duration | 656 | 33 | 0 | 88 | 10 |
| code lists -> Flash thinking, abstracts | assay | 787 | 0 | 0 | 77 | 0 |
| code lists -> Flash thinking, abstracts | target | 787 | 35 | 0 | 10 | 15 |
| code lists -> Flash thinking, abstracts | control_type | 787 | 6 | 0 | 0 | 28 |
| code lists -> Flash thinking, abstracts | sex | 787 | 168 | 0 | 8 | 0 |
| code lists -> Flash thinking, abstracts | age | 787 | 88 | 0 | 11 | 24 |
| code lists -> Flash thinking, abstracts | development_stage | 787 | 297 | 0 | 42 | 65 |
| code lists -> Flash thinking, abstracts | tissue | 787 | 56 | 0 | 72 | 46 |
| code lists -> Flash thinking, abstracts | cell_type | 787 | 25 | 0 | 0 | 12 |
| code lists -> Flash thinking, abstracts | cell_line | 787 | 24 | 0 | 0 | 43 |
| code lists -> Flash thinking, abstracts | disease | 787 | 227 | 0 | 4 | 252 |
| code lists -> Flash thinking, abstracts | genotype | 787 | 167 | 2 | 13 | 0 |
| code lists -> Flash thinking, abstracts | strain | 787 | 77 | 0 | 54 | 0 |
| code lists -> Flash thinking, abstracts | treatment | 787 | 62 | 1 | 72 | 140 |
| code lists -> Flash thinking, abstracts | dose | 787 | 121 | 0 | 0 | 45 |
| code lists -> Flash thinking, abstracts | duration | 787 | 152 | 9 | 0 | 24 |
| Flash, abstracts -> Flash thinking, abstracts | assay | 752 | 0 | 0 | 76 | 0 |
| Flash, abstracts -> Flash thinking, abstracts | target | 752 | 0 | 0 | 29 | 33 |
| Flash, abstracts -> Flash thinking, abstracts | control_type | 752 | 0 | 0 | 0 | 6 |
| Flash, abstracts -> Flash thinking, abstracts | sex | 752 | 49 | 27 | 57 | 0 |
| Flash, abstracts -> Flash thinking, abstracts | age | 752 | 14 | 0 | 17 | 0 |
| Flash, abstracts -> Flash thinking, abstracts | development_stage | 752 | 62 | 105 | 134 | 23 |
| Flash, abstracts -> Flash thinking, abstracts | tissue | 752 | 17 | 0 | 104 | 45 |
| Flash, abstracts -> Flash thinking, abstracts | cell_type | 752 | 12 | 8 | 0 | 12 |
| Flash, abstracts -> Flash thinking, abstracts | cell_line | 752 | 0 | 0 | 0 | 12 |
| Flash, abstracts -> Flash thinking, abstracts | disease | 752 | 22 | 64 | 199 | 107 |
| Flash, abstracts -> Flash thinking, abstracts | genotype | 752 | 11 | 12 | 95 | 2 |
| Flash, abstracts -> Flash thinking, abstracts | strain | 752 | 30 | 20 | 69 | 0 |
| Flash, abstracts -> Flash thinking, abstracts | treatment | 752 | 0 | 0 | 132 | 58 |
| Flash, abstracts -> Flash thinking, abstracts | dose | 752 | 12 | 2 | 15 | 46 |
| Flash, abstracts -> Flash thinking, abstracts | duration | 752 | 12 | 28 | 21 | 31 |

### Against the five example readings (differences only; the examples are not a gold standard)

**GSE100021**: example factors treatment ['igf1', 'control_vector']
| Chain | Ending | Factors written | Sample-label texts that differ | Where (label, samples) |
|---|---|---|---|---|
| code lists | settled | none | 48/128 | development_stage 8, tissue 8, disease 8, strain 8, treatment 8, dose 8 |
| Flash, abstracts | settled | treatment ['IGF1_adenovector', 'DsRed_adenovector'] | 40/128 | development_stage 8, tissue 8, strain 8, treatment 8, dose 8 |
| Flash, full text | settled | treatment ['IGF-I', 'DsRed'] | 56/128 | development_stage 8, tissue 8, genotype 8, strain 8, treatment 8, dose 8, duration 8 |
| Flash thinking, abstracts | settled | treatment ['igf1_adenovector', 'dsred_empty_vector'] | 48/128 | sex 8, development_stage 8, tissue 8, strain 8, treatment 8, dose 8 |
| Flash thinking, full text | settled | treatment ['IGF-I', 'DsRed'] | 40/128 | development_stage 8, tissue 8, disease 8, strain 8, dose 8 |

**GSE112068**: example factors dose ['0ug', '2ug', '40ug'], time_point ['0d', '2h', '4h', '8h', '12h', '24h', '36h', '2d', '4d', '6d', '8d']
| Chain | Ending | Factors written | Sample-label texts that differ | Where (label, samples) |
|---|---|---|---|---|
| code lists | settled | treatment ['control', 'bleomycin'], dose ['0 ug/ml', '2 ug/ml', '40 ug/ml'], time ['0 day', '4 days', '8 days', '2 days', '2 h', '4 h', '6 days', '8 h', '12 h', '24 h', '36 h'] | 69/368 | development_stage 23, disease 23, duration 23 |
| Flash, abstracts | settled | treatment ['control', 'bleomycin'], dose ['0', '2', '40'], time_after_exposure ['0d', '2h', '4h', '8h', '12h', '24h', '36h', '2d', '4d', '6d', '8d'] | 46/368 | sex 23, development_stage 23 |
| Flash thinking, abstracts | settled | treatment ['control', 'bleomycin'], dose ['0', '2', '40'], time_after_exposure ['0_day', '2_h', '4_h', '8_h', '12_h', '24_h', '36_h', '2_days', '4_days', '6_days', '8_days'] | 72/368 | sex 23, development_stage 23, disease 23, duration 3 |

**GSE235532**: example factors genotype ['w1118', 'LKRSDH_MB01942'], age ['3d', '40d']
| Chain | Ending | Factors written | Sample-label texts that differ | Where (label, samples) |
|---|---|---|---|---|
| code lists | unsettled | age ['3 day', '40 day'], genotype ['LKRSDHMB01942 mutant', 'w1118'], target ['input', 'H3K27me3', 'H3R17me2'] | 112/384 | sex 24, development_stage 24, disease 24, strain 24, target 8, control_type 8 |
| Flash, abstracts | unsettled | target ['input', 'H3K27me3', 'H3R17me2'], genotype ['w1118', 'LKRSDHMB01942 mutant'], age ['3 day', '40 day'] | 36/384 | sex 24, target 8, strain 4 |
| Flash, full text | unsettled | genotype ['w1118', 'LKRSDHMB01942'], age ['3_day', '40_day'], target ['input', 'H3K27me3', 'H3R17me2'] | 9/384 | target 8, control_type 1 |
| Flash thinking, abstracts | settled | age ['3_day', '40_day'], genotype ['w1118', 'LKRSDHMB01942_mutant'], target ['H3K27me3', 'H3R17me2'] | 24/384 | strain 24 |
| Flash thinking, full text | settled | age ['3_day', '40_day'], genotype ['w1118', 'LKRSDHMB01942'], target ['H3K27me3', 'H3R17me2'] | 0/384 |  |

**GSE117724**: example factors treatment ['DMSO', 'R24'], genotype ['wild-type', 'tm2590', 'ums12']
| Chain | Ending | Factors written | Sample-label texts that differ | Where (label, samples) |
|---|---|---|---|---|
| code lists | settled | strain ['N2', 'VL648'], treatment ['DMSO', 'R24'], genotype ['wild-type', 'nhr-86(tm2590)', 'nhr-86(ums12)', 'VL648 unc-119(ed3); wwIs22 [Pnhr-86::nhr-86ORF::GFP unc-119('], source_name ['WT_DMSO, whole animal', 'WT_R24, whole animal', 'nhr-86(tm2590) DMSO, whole animal', 'nhr-86(tm2590) R24, whole animal', 'nhr-86(ums12) DMSO, whole animal', 'nhr-86(ums12) R24, whole animal', 'VL648_DMSO, whole animal', 'VL648_R24, whole animal'] | 96/320 | sex 20, disease 20, dose 20, duration 20, strain 12, target 2, genotype 2 |
| Flash, abstracts | settled | treatment ['DMSO', 'R24'], genotype ['wild-type', 'nhr-86(tm2590)', 'nhr-86(ums12)', 'NHR-86::GFP'] | 48/320 | sex 20, strain 12, dose 10, target 2, genotype 2, duration 2 |
| Flash, full text | settled | genotype ['wild_type', 'nhr_86_tm2590', 'nhr_86_ums12', 'VL648_transgenic'], treatment ['DMSO', 'R24'] | 28/320 | strain 12, dose 10, target 2, genotype 2, duration 2 |
| Flash thinking, abstracts | settled | genotype ['wild_type', 'tm2590', 'ums12', 'vl648'], treatment ['DMSO', 'R24'] | 47/320 | sex 20, strain 12, dose 11, genotype 2, duration 2 |
| Flash thinking, full text | settled | treatment ['DMSO', 'R24'], genotype ['wild-type', 'nhr86_tm2590', 'nhr86_ums12', 'VL648_transgene'] | 63/320 | sex 20, duration 18, strain 12, dose 9, target 2, genotype 2 |

**GSE241718**: example factors age ['young', 'aged'], tissue ['SAT', 'VAT']
| Chain | Ending | Factors written | Sample-label texts that differ | Where (label, samples) |
|---|---|---|---|---|
| code lists | settled | tissue ['VAT', 'SAT'], age ['2 months', '24 months'] | 60/128 | assay 8, sex 8, development_stage 8, cell_type 8, disease 8, genotype 8, strain 8, age 4 |
| Flash, abstracts | settled | tissue ['VAT', 'SAT'], age ['2 months', '24 months'] | 28/128 | development_stage 8, cell_type 8, disease 8, age 4 |
| Flash, full text | settled | age ['2m', '24m'], tissue ['SAT', 'VAT'] | 28/128 | assay 8, development_stage 8, cell_type 8, age 4 |
| Flash thinking, abstracts | settled | age ['2_months', '24_months'], tissue ['SAT', 'VAT'] | 36/128 | development_stage 8, cell_type 8, disease 8, genotype 8, age 4 |
| Flash thinking, full text | settled | age ['young_2m', 'aged_24m'], tissue ['SAT', 'VAT'] | 36/128 | assay 8, development_stage 8, cell_type 8, genotype 8, age 4 |

### Format gaps over the 50

Counted by code from the records and the paper files (studies of 50):

| Gap | Studies |
|---|---|
| term_missing: assay | 50 |
| series_membership | 7 |
| sequencer | 6 |
| term_missing: target | 5 |
| pdf_full_text | 4 |
| term_missing: rat stage | 2 |
| term_missing: fly stage (FBdv is in OLS4 but the format names no fly list) | 2 |
| batch | 2 |

Counted by code from the written readings (thinking + full text where held, else thinking + abstracts; studies with a reading):

| Gap | Studies |
|---|---|
| text_said_once | 50 |
| group_smaller_than_study | 38 |
| pooled_material | 21 |
| crossed_not_fully | 19 |
| two_beliefs_one_claim (a level and an exact value) | 19 |
| factor_over_part | 11 |
| two_beliefs_one_claim (shared material and control tie) | 2 |

Jev's yes (p >= 0.5) to a yes/no probe over the records and abstracts (studies of 50):

| Probe | Studies |
|---|---|
| held_constant | 50 |
| untreated_or_vehicle | 37 |
| pooled_material | 27 |
| time_from_event | 18 |
| sources_disagree | 16 |
| single_cell | 16 |
| route_or_side | 12 |
| batch | 9 |
| duration_without_number | 7 |
| sequencer | 7 |
| mixed_assays | 7 |
| several_compounds | 6 |
| dose_per_unit | 6 |
| vector | 5 |
| one_control_many_materials | 1 |
| baseline_as_control | 1 |

The writer's own format_gaps list (studies naming each kind):

| Kind | Flash, abstracts | Flash, full text | Flash thinking, abstracts | Flash thinking, full text |
|---|---|---|---|---|
| archive_biosample_differs | 0 | 0 | 1 | 1 |
| baseline_as_control | 16 | 4 | 15 | 16 |
| batch | 5 | 3 | 12 | 15 |
| crossed_not_fully | 1 | 3 | 22 | 18 |
| dose_per_unit | 5 | 2 | 4 | 7 |
| duration_without_number | 4 | 10 | 8 | 6 |
| factor_over_part | 19 | 10 | 24 | 19 |
| group_smaller_than_study | 4 | 8 | 7 | 16 |
| held_constant | 13 | 10 | 7 | 6 |
| one_control_many_materials | 1 | 0 | 3 | 3 |
| other | 4 | 2 | 23 | 14 |
| pooled_material | 13 | 10 | 19 | 18 |
| route_or_side | 1 | 2 | 0 | 1 |
| sequencer | 0 | 0 | 6 | 4 |
| series_membership | 0 | 1 | 6 | 9 |
| several_compounds | 8 | 3 | 4 | 3 |
| sources_disagree | 6 | 10 | 18 | 28 |
| term_missing | 31 | 21 | 26 | 29 |
| time_from_event | 4 | 2 | 9 | 10 |
| treatment | 0 | 1 | 0 | 0 |
| two_beliefs_one_claim | 2 | 2 | 0 | 1 |
| untreated_or_vehicle | 2 | 0 | 7 | 7 |

### By assay

| Assays in the study | Studies | code lists (settled/unsettled/refused) | Flash, abstracts (settled/unsettled/refused) | Flash, full text (settled/unsettled/refused) | Flash thinking, abstracts (settled/unsettled/refused) | Flash thinking, full text (settled/unsettled/refused) |
|---|---|---|---|---|---|---|
| RNA-Seq | 37 | 27/9/1 | 25/9/3 | 24/5/2 | 29/8/0 | 25/6/0 |
| ChIP-Seq | 3 | 2/1/0 | 1/2/0 | 1/1/1 | 3/0/0 | 2/1/0 |
| OTHER + RNA-Seq | 3 | 1/2/0 | 2/1/0 | 2/0/0 | 1/2/0 | 2/0/0 |
| ChIP-Seq + RNA-Seq | 2 | 2/0/0 | 1/1/0 | 1/1/0 | 1/1/0 | 1/1/0 |
| Bisulfite-Seq | 2 | 2/0/0 | 2/0/0 | 1/1/0 | 2/0/0 | 2/0/0 |
| miRNA-Seq | 1 | 0/1/0 | 1/0/0 | 1/0/0 | 1/0/0 | 0/1/0 |
| ATAC-seq | 1 | 1/0/0 | 1/0/0 | 0/0/0 | 1/0/0 | 0/0/0 |
| RNA-Seq + ncRNA-Seq | 1 | 0/1/0 | 0/1/0 | 1/0/0 | 1/0/0 | 1/0/0 |

Assay text written per study for the assays other than plain RNA-Seq (each chain):

- GSE117724 (ChIP-Seq, RNA-Seq): code lists: RNA-Seq x18, ChIP-Seq x2; Flash, abstracts: RNA-Seq x18, ChIP-Seq x2; Flash, full text: RNA-Seq x18, ChIP-Seq x2; Flash thinking, abstracts: RNA-Seq x18, ChIP-Seq x2; Flash thinking, full text: RNA-Seq x18, ChIP-Seq x2
- GSE125850 (ChIP-Seq): code lists: ChIP-Seq x12; Flash, abstracts: ChIP-Seq x12; Flash, full text: ChIP-Seq x12; Flash thinking, abstracts: ChIP-Seq x12; Flash thinking, full text: ChIP-Seq x12
- GSE129928 (ChIP-Seq): code lists: ChIP-Seq x12; Flash, abstracts: ChIP-Seq x12; Flash, full text: refused; Flash thinking, abstracts: ChIP-Seq x12; Flash thinking, full text: ChIP-Seq x12
- GSE131269 (OTHER, RNA-Seq): code lists: RNA-Seq x12, OTHER x12; Flash, abstracts: mRNAseq x12, RiboSeq x12; Flash, full text: RNA-Seq x12, Ribosomal profiling x12; Flash thinking, abstracts: RNA-Seq x12, Ribosomal profiling x12; Flash thinking, full text: RNA-Seq x12, Ribosomal profiling x12
- GSE146013 (miRNA-Seq): code lists: miRNA-Seq x24; Flash, abstracts: miRNA-Seq x24; Flash, full text: miRNA-Seq x24; Flash thinking, abstracts: miRNA-Seq x24; Flash thinking, full text: miRNA-Seq x24
- GSE147401 (ChIP-Seq, RNA-Seq): code lists: ChIP-Seq x24, RNA-Seq x6; Flash, abstracts: ChIP-Seq x24, RNA-Seq x6; Flash, full text: ChIP-Seq x24, RNA-Seq x6; Flash thinking, abstracts: ChIP-Seq x24, RNA-Seq x6; Flash thinking, full text: ChIP-Seq x24, RNA-Seq x6
- GSE175410 (Bisulfite-Seq): code lists: Bisulfite-Seq x15; Flash, abstracts: Bisulfite-Seq x15; Flash, full text: Bisulfite-Seq x15; Flash thinking, abstracts: Bisulfite-Seq x15; Flash thinking, full text: Bisulfite-Seq x15
- GSE235532 (ChIP-Seq): code lists: ChIP-Seq x24; Flash, abstracts: ChIP-Seq x24; Flash, full text: ChIP-Seq x24; Flash thinking, abstracts: ChIP-Seq x24; Flash thinking, full text: ChIP-Seq x24
- GSE272899 (ATAC-seq): code lists: ATAC-seq x9; Flash, abstracts: ATAC-seq x9; Flash, full text: -; Flash thinking, abstracts: ATAC-seq x9; Flash thinking, full text: -
- GSE274290 (RNA-Seq, ncRNA-Seq): code lists: RNA-Seq x1, ncRNA-Seq x6; Flash, abstracts: BD Rhapsody™ Single-Cell Analysis System x1, ncRNA-Seq x6; Flash, full text: RNA-Seq x1, ncRNA-Seq x6; Flash thinking, abstracts: RNA-Seq x1, ncRNA-Seq x6; Flash thinking, full text: scRNA-seq x1, smRNA-seq x6
- GSE288182 (Bisulfite-Seq): code lists: Bisulfite-Seq x9; Flash, abstracts: Bisulfite-Seq x9; Flash, full text: Bisulfite-Seq x9; Flash thinking, abstracts: Bisulfite-Seq x9; Flash thinking, full text: Bisulfite-Seq x9
- GSE329430 (OTHER, RNA-Seq): code lists: RNA-Seq x15, OTHER x5; Flash, abstracts: single cell RNA-Seq x7, CMO x2, HTO x2, ADT x2, bulk RNA-Seq x7; Flash, full text: -; Flash thinking, abstracts: single-cell RNA sequencing x5, CMO x2, HTO x3, ADT x3, bulk RNAseq x7; Flash thinking, full text: -
- GSE97461 (OTHER, RNA-Seq): code lists: RNA-Seq x16, OTHER x3; Flash, abstracts: RNA-Seq x16, OTHER x3; Flash, full text: RNA-Seq x16, OTHER x3; Flash thinking, abstracts: RNA-Seq x16, ribosome footprinting x3; Flash thinking, full text: RNA-Seq x16, ribosome footprinting x3

Pull-down studies: target and control libraries as written (thinking + full text, else thinking):

- GSE117724 code lists: target {'<na>': 18, 'anti-GFP antibody (Roche Cat#11814460001, Lot#19958500)': 2}; control_type {'<na>': 20}; factors ['strain', 'treatment', 'genotype', 'source_name']; 20 biosample ids for 20 samples
- GSE117724 Flash, abstracts: target {'<na>': 18, 'anti-GFP antibody (Roche Cat#11814460001, Lot#19958500)': 2}; control_type {'<na>': 20}; factors ['treatment', 'genotype']; 20 biosample ids for 20 samples
- GSE117724 Flash, full text: target {'<na>': 18, 'GFP': 2}; control_type {'<na>': 20}; factors ['genotype', 'treatment']; 20 biosample ids for 20 samples
- GSE117724 Flash thinking, abstracts: target {'<na>': 18, 'NHR-86': 2}; control_type {'<na>': 20}; factors ['genotype', 'treatment']; 20 biosample ids for 20 samples
- GSE117724 Flash thinking, full text: target {'<na>': 18, 'NHR-86::GFP': 2}; control_type {'<na>': 20}; factors ['treatment', 'genotype']; 20 biosample ids for 20 samples
- GSE125850 code lists: target {'<unknown>': 12}; control_type {'<unknown>': 12}; factors ['disease']; 12 biosample ids for 12 samples
- GSE125850 Flash, abstracts: target {'<na>': 6, 'H3K9me2': 6}; control_type {'input': 6, 'ChIP': 6}; factors ['treatment', 'control_type']; 12 biosample ids for 12 samples
- GSE125850 Flash, full text: target {'<na>': 5, 'H3K9me2': 7}; control_type {'input': 6, '<na>': 6}; factors ['genotype', 'treatment']; 11 biosample ids for 12 samples
- GSE125850 Flash thinking, abstracts: target {'<na>': 6, 'H3K9Me2': 6}; control_type {'input': 6, '<na>': 6}; factors ['condition']; 11 biosample ids for 12 samples
- GSE125850 Flash thinking, full text: target {'<na>': 6, 'H3K9Me2': 6}; control_type {'input': 7, '<na>': 5}; factors ['genotype', 'treatment']; 11 biosample ids for 12 samples
- GSE129928 code lists: target {'<unknown>': 12}; control_type {'<na>': 12}; factors ['strain']; 12 biosample ids for 12 samples
- GSE129928 Flash, abstracts: target {'H3K9me2 antibody (ab1220, Abcam)': 10, '<na>': 2}; control_type {'input': 12}; factors ['genotype', 'generation']; 12 biosample ids for 12 samples
- GSE129928 Flash, full text: no reading (refused)
- GSE129928 Flash thinking, abstracts: target {'H3K9me2': 10, '<na>': 2}; control_type {'input': 12}; factors ['genotype', 'generation']; 12 biosample ids for 12 samples
- GSE129928 Flash thinking, full text: target {'H3K9me2': 10, '<na>': 2}; control_type {'input': 12}; factors ['genotype', 'generation']; 12 biosample ids for 12 samples
- GSE147401 code lists: target {'H3K27me3': 8, 'H3K36me3': 8, 'none': 14}; control_type {'<na>': 30}; factors ['tissue', 'target', 'strain', 'genotype']; 30 biosample ids for 30 samples
- GSE147401 Flash, abstracts: target {'H3K27me3': 8, 'H3K36me3': 8, '<na>': 14}; control_type {'<na>': 22, 'input': 8}; factors ['genotype', 'target']; 29 biosample ids for 30 samples
- GSE147401 Flash, full text: target {'H3K27me3': 8, 'H3K36me3': 8, 'none': 14}; control_type {'<na>': 22, 'input': 8}; factors ['assay', 'target', 'genotype']; 27 biosample ids for 30 samples
- GSE147401 Flash thinking, abstracts: target {'H3K27me3': 8, 'H3K36me3': 8, 'antibody: none': 8, '<na>': 6}; control_type {'<na>': 22, 'input': 8}; factors ['assay', 'target', 'genotype']; 28 biosample ids for 30 samples
- GSE147401 Flash thinking, full text: target {'H3K27me3': 8, 'H3K36me3': 8, 'none': 8, '<na>': 6}; control_type {'<na>': 22, 'input': 8}; factors ['genotype', 'target', 'tissue']; 28 biosample ids for 30 samples
- GSE235532 code lists: target {'input': 8, 'H3K27me3': 8, 'H3R17me2': 8}; control_type {'<na>': 24}; factors ['age', 'genotype', 'target']; 24 biosample ids for 24 samples
- GSE235532 Flash, abstracts: target {'input': 8, 'H3K27me3': 8, 'H3R17me2': 8}; control_type {'input': 8, '<na>': 16}; factors ['target', 'genotype', 'age']; 24 biosample ids for 24 samples
- GSE235532 Flash, full text: target {'input': 8, 'H3K27me3': 8, 'H3R17me2': 8}; control_type {'input': 9, '<na>': 15}; factors ['genotype', 'age', 'target']; 24 biosample ids for 24 samples
- GSE235532 Flash thinking, abstracts: target {'<na>': 8, 'H3K27me3': 8, 'H3R17me2': 8}; control_type {'input': 8, '<na>': 16}; factors ['age', 'genotype', 'target']; 24 biosample ids for 24 samples
- GSE235532 Flash thinking, full text: target {'<na>': 8, 'H3K27me3': 8, 'H3R17me2': 8}; control_type {'input': 8, '<na>': 16}; factors ['age', 'genotype', 'target']; 24 biosample ids for 24 samples

## Files

| File | Holds | Committed |
|---|---|---|
| `material.py` | reads a study from the toy store; text views; code's quote finder | yes |
| `ontology.py` | OLS4 and Cellosaurus candidates, cached, 3 requests per second at most | yes |
| `chain.py` | the chain: lists, Jev questions, assembly, ending | yes |
| `run.py` | runs one variant over the 50 | yes |
| `summarize.py` | the tables here, and `per_study.md` | yes |
| `per_study.md` | cost, time and ending per study per chain | yes |
| `out/<variant>/<ID>.reading.yaml` | a reading that passed the format check | yes |
| `out/<variant>/<ID>.refused.yaml` | a refused study and its problems | yes |
| `out/<variant>/<ID>.result.json` | per study: factors and Jev's answers, ask-twice differences, every claim's p and backing, quote checks, ontology picks, gap probes, cost and time | yes |
| `_logs/`, `_cache/` | raw model replies, the study material with paper text, OLS4/Cellosaurus responses | **no**, git-ignored (paper text is private) |

Rerun from this folder with seqforge's pixi Python, the tunnel up and `LABLLM_GATEWAY`,
`LABDATA_STORE`, `LABDATA_PAPERS` set:

```bash
python material.py                 # cache the 50 studies' material
python run.py --chain code
python run.py --chain flash [--full]
python run.py --chain flash_think [--full]
python summarize.py                # prints the tables; writes per_study.md
```
