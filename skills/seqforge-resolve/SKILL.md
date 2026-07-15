---
name: seqforge-resolve
description: >-
  Score bytes + KB into a ranked chemistry decision with `seqforge resolve
  score`, and adjudicate ONLY the conflicts code already flagged. Use when
  asked which chemistry/technology a dataset is, why resolve refused, or to
  answer an open question in questions.md. The scoring is deterministic and has
  no LLM in it — your role begins only after code has flagged an ambiguity.
---

# seqforge resolve

```bash
seqforge resolve score FILES --json [--explain] [--assert-chemistry ID]
seqforge resolve apply --question-id ID --chosen VALUE --actor user
seqforge resolve adjudicate           # opt-in LLM job (b); OFF in `run` by default
```

`--explain` emits the evidence matrix `M[role][file]` — read it when asked *why*.

## Your lane

Scoring is **deterministic and has no LLM in it**. You do not rank candidates, and you do not decide
a chemistry. You act only on what code has already flagged:

- **exit 0** — decided. Nothing for you to do.
- **exit 3** — a `Blocker`. Read `remedy` and report it. Do not route around it.
- **exit 4** — an open `Conflict` or question. **This is the only place you have a job.**

## On exit 4

The choice set is fixed by code. Your job is to help a human pick *within* it — never to widen it,
and never to pick for them unless `adjudicate` is explicitly enabled (it is off in `run` by design).

Two things worth knowing when you explain a conflict:

**A conflict is not a bug.** `observed_vs_asserted` means the bytes and the metadata genuinely
disagree — e.g. metadata says v2, reads are 28 bp. The library takes the observed value because
authority follows evidence, and the disagreement stays attached because deleting it would make the
manifest read as though nothing ever disagreed. Three truths, never merged.

**Some ties are benign, and code already knows it.** v3 vs v3.1 emit byte-identical params, so both
ids are recorded and **zero** questions are asked (§12). If you are being asked, the tie is
*processing-divergent* — the answer changes what runs — so it genuinely needs a human.

## The thing not to do

Do not resolve an open question by reasoning about which answer is more likely. An unanswered
question that gets quietly settled is exactly how a wrong manifest reaches the corpus, and the Stop
hook exists because that temptation is real. Ambiguity clears two ways: a human answers, or code
decides. There is no third.
