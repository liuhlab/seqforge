---
name: seqforge-harvest
description: >-
  Turn prose (methods sections, GEO/SRA records, READMEs, manuscripts) into
  span-verified Assertions via `seqforge harvest normalize|extract|verify`. Use
  when a dataset comes with any human-written description and you need machine
  claims out of it. This is the ONE LLM touchpoint in the whole compiler. Run as
  a subagent — documents are long and only the compact result should return.
---

# seqforge harvest

The only stage where a model proposes anything. Everything else in seqforge is a verifier.

```bash
seqforge harvest normalize DOCS               # -> canonical span space (deterministic)
seqforge harvest extract DOCS --verify        # -> AssertionDraft[] -> Assertion[]  (LLM)
seqforge harvest verify DRAFTS                # -> the R5 tripwire, on its own
```

Providers: `--provider anthropic|deepseek|openai-compatible`, `--model ...`. Auto-detects
`DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` and **refuses rather than guessing** when neither is set.

## Document ROLE: only a file you hand us may steer the pipeline

```bash
seqforge harvest extract PAPER.pdf --instruction notes.md
#                        ^ reference             ^ instruction
```

A **reference** document is one you cite; it may set `library.*`/`experiment.*` and nothing else. An
**instruction** document is one you wrote *for seqforge*; it may additionally set
`processing.quantification` and `processing.genome.assembly`.

Three things about this are load-bearing:

1. **Role is the flag, never the filename.** `alignment_instruction.md` is a convention you pass to
   `--instruction`; it is load-bearing nowhere. A filename trigger would be spoofable by renaming a
   downloaded PDF.
2. **A downloaded methods PDF may never set `processing.*`.** A GEO description is an untrusted
   input, and prose reaching `--soloStrand` is prompt injection from a database field into an
   aligner. `verify` rejects it (`field_not_permitted_for_doc_role`) regardless of what was asked.
3. **You are not classifying mood.** "we used GeneFull" (declarative) and "align this in GeneFull
   mode" (imperative) are treated identically — role subsumes mood by fiat, because a mood judgement
   has no quote to check it against and would land in exactly the class R5 is blind to.

An instruction **promotes; it never narrows.** "Align in GeneFull mode" makes GeneFull the primary
matrix; it does not drop the other four. So a hallucinated instruction can mislabel which matrix is
primary — it cannot destroy signal. That is the safety property that makes this path acceptable at
all.

Name the STARsolo feature. "count introns too" is **correctly rejected** as not-entailed: inferring
`nuclei -> GeneFull` is an inference code owns, not the model, and teaching `entails` that alias
would make R5 theatre on the one field where it is not vacuous.

## What the model is allowed to do

Emit `{field, value, quote}`. That is all. It does **not** emit character offsets (it cannot count
characters — code computes them), does not assert its own quote is real, and does not decide what
survives. Code overwrites `span.doc_sha256`, because we know which document we sent.

## The rule that matters

**Extract only what the document explicitly states.** Never infer, never complete a pattern, never
use background knowledge. If the document does not state a field, omit it — an empty result is
correct and common.

This is harder than it sounds, and the failure is seductive. A document saying "droplet-based
single-cell RNA-seq using a commercial microfluidic platform" is almost certainly 10x 3′ v3. Saying
so is still wrong: the document does not state it, and a guess that happens to land is
indistinguishable from knowledge until the day it doesn't. Downstream code re-greps every quote and
checks it entails the value, so a stretched claim gains nothing — it only wastes the extraction.

## Know what verification cannot do

`verify` catches **fabricated** quotes (not in the document) and **mis-attributed** ones (the quote
does not entail the value). It cannot catch a **field-assignment error**: for a free-text field the
value is copied out of the quote, so entailment is trivially satisfied. A real quote filed under the
wrong field passes by construction.

That is not hypothetical — the evals caught a model filing standard worm husbandry ("maintained on
NGM plates seeded with E. coli OP50 at 20 C") as an experimental `condition`. A true sentence,
correctly quoted, in a field it does not belong in. Only the prompt's field definitions and the evals
corpus defend that boundary. So: read the field definitions in the prompt and honour "omit it".

## Return

The accepted `Assertion`s and the rejection reasons — never the document.
