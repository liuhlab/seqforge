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
seqforge resolve score FILES [--explain] [--assert-chemistry ID]   # the LIBRARY, from bytes
```

`--explain` emits the evidence matrix `M[role][file]` — read it when asked *why*. No `--json` flag:
JSON on stdout is the default (R8).

`resolve apply` and `resolve adjudicate` are **not built**. This skill listed both as if they were,
which is a confident instruction to fail — found 2026-07-16 when the verb guard learned to check
subcommands. `adjudicate` is LLM job (b) and is modelled (`ArbitrationRequest`/`ArbitrationResponse`)
but has no verb.

## There are two resolvers

`resolve score` answers **"what IS this library?"** from bytes. It is what this skill is about.

`resolve_metadata` answers **"which sample is each file, and what was that sample?"** from archive
records and prose — it is a sibling, not a helper, and `manifest fill` runs it. It is handed
`FileIdentity`, never `Observation`, so it cannot see a probe signal: there is no byte in a FASTQ that
bears on `tissue` or `strain`, and a probe-sighted reader would settle ties the probe itself created
and log the wrong reason.

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
