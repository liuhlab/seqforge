# Passes and reviews

Some of seqforge's work asks a language model the same kind of question many times: once for each
GEO Series, about 200,000 of them. seqforge runs that work as **passes**. A pass runs one **unit
kind** over a list of **units**, each named by a key, and records how each unit ended. The engine
behind it is the lab package [liulab-llms](https://liuhlab.github.io/liulab-llms/). It sends every
call through the lab gateway, caches every answer, stops at a dollar ceiling, and keeps the record
in a **task** folder. seqforge names its kinds and starts their passes. The engine's words are
defined in [liulab-llms's glossary](https://github.com/liuhlab/liulab-llms/blob/main/GLOSSARY.md).

seqforge has no kind of its own yet. The first one comes with the work that reads studies.

## Before you start

You need two things:

- **A tunnel to the lab gateway**, and `LABLLM_GATEWAY` set to its local address, such as
  `http://127.0.0.1:8000`. [Reach the lab gateway](https://liuhlab.github.io/liulab-llms/reach-the-gateway/)
  shows how to open one.
- **A local clone of the lab's review repo.** Give it as `--reviews DIR`, or set
  `SEQFORGE_REVIEWS` to it. A pass reads the reviews in its `reviews/` folder to learn which units
  a person has already answered.

## Start a pass

```bash
seqforge pass start <kind> --ceiling-usd 5
```

This runs the kind over its default units, in the kind's own order. Each unit ends in one of five
ways:

| Ending | What it means |
| --- | --- |
| settled | The answer passed every check |
| unsettled | The answer failed a check, and a person or a stronger model should look |
| refused | The kind's code declined to answer the unit |
| unfinished | The pass stopped, or the unit's code failed, before it ended |
| held | An input the unit needs is missing |

The options:

| Option | What it does |
| --- | --- |
| `--ceiling-usd` | The most the pass may spend, in dollars. Required: it has no default |
| `--model ROLE=PROVIDER/NAME[,KEY=VALUE...]` | The model for one role, such as `writer=deepseek/deepseek-v4-flash,thinking=off`. Repeat it for more roles. The keys are `thinking` (`on` or `off`), `effort`, `temperature` and `max-output-tokens`. A role you do not name takes the kind's default model |
| `--keys FILE` | Run exactly these units, one key per line, in this order |
| `--only ENDINGS` | Run only the units whose current ending is one of these, such as `unsettled,refused` |
| `--units-at-once` | How many units run at once. Default 32 |
| `--off-peak-only` | Send calls only outside the model's peak hours |
| `--label` | The pass's name in the gateway's record. Default: the kind's name |
| `--task DIR` | The task folder. Default: `seqforge/passes/<kind>/` in the current folder |
| `--reviews DIR` | The review repo's clone, if `SEQFORGE_REVIEWS` does not name it |

The gateway has no model aliases, so `PROVIDER/NAME` is the provider's exact model name. A `--model`
value is the whole model: a setting you leave out takes liulab-llms's default, not the kind's.

### What it prints

While the pass runs, a progress line goes to stderr at most every 30 seconds. It gives the units
ended so far, by ending, and the dollars spent. stdout carries one JSON object and nothing else:

| Field | Value |
| --- | --- |
| `kind`, `kind_version` | The kind, and its hand-set version |
| `task` | The task folder |
| `pass_id`, `label` | The pass's number in the task, and its label |
| `store_build` | The liulab-data store build the pass read, or `null` for a kind that reads no store |
| `models` | Each role's full model value |
| `keys` | How many units the pass was given |
| `counts` | How many units ended each way: settled, unsettled, refused, unfinished and held, zeros included |
| `stopped`, `message` | Why the pass stopped, and the gateway's message about it; both `null` when it reached its end |
| `spent_usd`, `first_cost_usd` | What the pass spent, and what its answers cost when each was first paid, answers from the cache included |
| `requests`, `cache_hits` | Requests sent to providers, and answers that came from the cache |

A kind that reads liulab-data's store opens it once, when the pass starts. The whole pass reads that
one build, even if the store is refreshed while it runs.

### Exit codes

| Exit | When |
| --- | --- |
| 0 | The pass reached the end of its units, whatever the counts |
| 2 | A setup it cannot use: an unknown kind, models that do not fill the kind's roles, a model or keys file it cannot read, no review clone, no gateway address, or a task folder this liulab-llms cannot read. One line on stderr says which |
| 3 | The pass stopped: at its ceiling, at a provider's balance or cap, or because a provider or the gateway was unavailable. Or another process holds the task, and `message` names it |

## Resume a pass

Run the same command again. There is no resume option. Every unit answered before comes back from
the gateway's cache at no cost, and `cache_hits` counts them. After a stop at the ceiling, raise
`--ceiling-usd` and run it again.

## Send the hard units to a stronger model

After a pass, rerun only the units that did not settle, on a stronger model:

```bash
seqforge pass start <kind> --ceiling-usd 5 --only unsettled,refused --model writer=deepseek/deepseek-v4-pro
```

A unit a person has already reviewed is left out, as long as its inputs have not changed since the
review.
