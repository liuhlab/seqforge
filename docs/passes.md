# Passes

Some of seqforge's work asks a language model the same kind of question once for each of many
studies. seqforge runs that work as a **pass**. A pass takes one **kind** of work and a list of
**units**, each named by a key such as a GEO Series id. It asks the models about each unit, and it
records how each unit ended. A pass never spends more than the dollar ceiling you give it.

The engine behind a pass is the lab package
[liulab-llms](https://github.com/liuhlab/liulab-llms). It sends every model call through the lab
gateway, the one server that holds the lab's model keys. It caches every answer, stops at the
ceiling, and keeps its record in a **task** folder. seqforge names its kinds and starts their
passes. The engine's own words are defined in
[liulab-llms's glossary](https://github.com/liuhlab/liulab-llms/blob/main/GLOSSARY.md).

seqforge has two kinds. Both work on a folder of study folders.

| Kind | What one unit does | Models it uses |
| --- | --- | --- |
| `study-bounds` | Draws one study's bounds: which GEO Series and papers belong to it. See [bound the study](study-workflow.md#1-bound-the-study). | `jev`: `typesafe/jev-1.14.0` |
| `study-reading` | Reads what each sequenced sample of one study is. See [read the study](study-workflow.md#3-read-the-study). | `writer`: `deepseek/deepseek-flash`, thinking on; `jev`: `typesafe/jev-1.14.0` |

Jev is a model that answers yes-or-no and multiple-choice questions with a probability. A model's
**role** is the job it does in a kind, such as `writer` or `jev`.

## What a pass needs

Every pass needs the lab gateway, and the two study kinds also read the lab's stores of archive
records, papers and ontologies. Each store is part of liulab-data, the lab's data package. Set
these variables before you start a pass:

| Variable | What it points to | Which kinds need it |
| --- | --- | --- |
| `$LABLLM_GATEWAY` | The lab gateway's local address, such as `http://127.0.0.1:8000`, served by a tunnel you open | Both |
| `LABDATA_STORE` | liulab-data's store of GEO and other archive records | Both |
| `LABDATA_PAPERS` | liulab-data's paper store: each paper's abstract, and its full text where the store holds one | Both, for any study that links a paper |
| `LABDATA_ONTOLOGIES` | liulab-data's ontology store, the vocabularies that a reading's ids come from | `study-reading` |

[Reach the lab gateway](https://github.com/liuhlab/liulab-llms/blob/main/docs/reach-the-gateway.md) shows how to
open the tunnel. Each liulab-data store can also be found through a pointer that liulab-data saves
for you, in place of its variable.

A kind opens each store once, when the pass starts. The whole pass reads that one build of the
record store and those ontology snapshots, even if a store is refreshed while the pass runs.

## Start a pass

```bash
seqforge pass start <kind> --ceiling-usd <dollars>
```

This command calls the gateway and reads the stores above. With no `--keys`, a study kind runs the
studies that the folder's `pick.yaml` lists, in that order. The folder is the standard set,
`evals/standard-set`, unless `--studies` names another.

The options:

| Option | What it does |
| --- | --- |
| `--ceiling-usd` | The most the pass may spend, in dollars. Required: it has no default. |
| `--model ROLE=PROVIDER/NAME[,KEY=VALUE...]` | The model for one role, such as `writer=deepseek/deepseek-v4-flash,thinking=off`. Repeat it for more roles. The keys are `thinking` (`on` or `off`), `effort`, `temperature` and `max-output-tokens`. |
| `--keys FILE` | Run exactly these units, one key per line, in this order. A key listed twice is refused. |
| `--studies DIR` | The folder of study folders that the pass works on. Default: `evals/standard-set`. |
| `--only ENDINGS` | Run only the units whose current ending is one of these, comma-separated, such as `unsettled,refused`. You cannot give both `--keys` and `--only`. |
| `--units-at-once` | How many units run at once. Default: 32. |
| `--off-peak-only` | Send calls only outside the model's peak hours. A call made in peak hours waits for them to end. |
| `--label` | The pass's name in the gateway's record. Default: the kind's name. |
| `--task DIR` | The task folder. Default: `seqforge/passes/<kind>/` in the current folder. |

A role that you do not name with `--model` takes the kind's default model. A role that you do name
takes exactly what you write: a setting you leave out takes liulab-llms's default, not the kind's.
The gateway has no short names for models, so `NAME` is the provider's exact model name.

### How a unit ends

Each unit ends in one of five ways:

| Ending | What it means |
| --- | --- |
| settled | The answer passed every check. |
| unsettled | The answer failed a check. A person or a stronger model should look at it. |
| refused | The kind's code declined to answer the unit, and said why. |
| unfinished | The pass stopped, or the unit's code failed, before the unit ended. |
| held | An input that the unit needs is missing, such as the study's bounds file. |

What makes a study settled, unsettled, refused or held is the kind's own rule.
[The study workflow](study-workflow.md) gives each kind's rules.

### The ceiling

The ceiling is a dollar amount for this one pass. Before each model call, the engine checks
whether the call's largest possible cost would cross the ceiling. If it would, the pass stops with
`ceiling_reached` and exits 3. An answer replayed from the gateway's cache costs nothing. A ceiling
must be a number of dollars, zero or more.

### What a pass prints

While the pass runs, it writes progress lines to stderr:

- one line as it starts: the kind, how many units, the ceiling, how many at once, and the task;
- a line at most every 30 seconds: the units ended so far, by ending, the dollars spent, and the
  time taken;
- one line as it ends: whether it reached the end of its units or stopped and why, the counts, the
  dollars spent, the requests sent, and the answers from the cache.

stdout carries one JSON object and nothing else:

| Field | Value |
| --- | --- |
| `kind`, `kind_version` | The kind, and its version. The version is set by hand and changes when a question or a rule of the kind changes. |
| `task` | The task folder. |
| `pass_id`, `label` | The pass's number in the task, and its label. |
| `store_build` | The build of liulab-data's record store that the pass read, or `null` for a kind that reads no record store. |
| `models` | Each role's full model. |
| `keys` | How many units the pass was given. |
| `counts` | How many units ended each way: settled, unsettled, refused, unfinished and held, zeros included. |
| `stopped`, `message` | Why the pass stopped, and the gateway's message about it. Both are `null` when the pass reached its end. |
| `spent_usd` | What the pass paid. |
| `first_cost_usd` | What its answers cost when each was first paid, answers from the cache included. |
| `requests`, `cache_hits` | Requests sent to providers, and answers that came from the cache. |

`stopped` is one of these:

| `stopped` | Why |
| --- | --- |
| `ceiling_reached` | The next call could cross the ceiling. |
| `provider_exhausted` | The provider's prepaid balance or monthly cap is used up. |
| `provider_unavailable` | The provider still failed after the gateway's retries. |
| `gateway_unreachable` | The gateway could not be reached. |
| `task_in_use` | Another process holds the task. `message` names it: who, which process, on which machine, and since when. |

### Exit codes

| Exit | When |
| --- | --- |
| 0 | The pass reached the end of its units, whatever the counts. |
| 2 | A setup the pass cannot use. One line on stderr says which. |
| 3 | The pass stopped: one of the reasons in the `stopped` table. |

A setup that exits 2 is one of these:

- an unknown kind;
- a store the kind reads that cannot be opened;
- a role the kind does not have, or a role with no model;
- a model text or a keys file it cannot read, or a key listed twice;
- both `--keys` and `--only`, or an ending `--only` does not know;
- no `--keys` for a folder that has no `pick.yaml`;
- no gateway address, or a ceiling that is not a number of dollars;
- a model name the gateway rejects;
- a task folder that this version of liulab-llms cannot read.

## Resume a pass

Run the same command again. There is no resume option. Every unit answered before comes back from
the gateway's cache at no cost, and `cache_hits` counts them. After a stop at the ceiling, raise
`--ceiling-usd` and run the command again.

Run it from the same folder, or give the same `--task`, so that the pass finds its task.

## See where a kind stands

```bash
seqforge pass status <kind>
```

This command reads the kind's task and prints one JSON object. It sends nothing to the gateway and
opens none of the stores, so it works with the gateway down. It takes no hold on the task, so it
also works while a pass runs. It takes `--task DIR` as `pass start` does.

| Field | Value |
| --- | --- |
| `kind`, `task` | The kind, and the task folder. |
| `units` | How many units any pass of the kind has recorded. |
| `counts` | Each unit's current ending: settled, unsettled, refused or dropped, a person's review included. Then the units whose latest outcome is unfinished or held. A unit settled once and unfinished in a later pass counts under both. |
| `not_done` | Each unit whose latest outcome is unfinished or held, in key order: its `key`, that `ending`, the outcome's `reason`, and the `pass` it came from. |
| `passes` | Every pass of the kind, oldest first. |

`dropped` is an ending only a person's review gives. `not_done` lists every such unit, with no
cap. A held study's `reason` says what it lacks, such as its bounds file. To see only the held
units, filter the list with `jq`, a tool that reads JSON:

```bash
seqforge pass status study-reading | jq '.not_done[] | select(.ending == "held")'
```

A refused or unsettled unit is not in `not_done`. `study write-bounds` and `study write-reading`
list each of those, with each refused study's reason. Unlike `pass status`, they refuse to run
while a pass holds the task: they exit 3 with `stopped: task_in_use`.

Each item of `passes` gives the same fields as `pass start` prints for its pass: `pass_id`,
`label`, `kind_version`, `models`, `store_build`, `counts`, `stopped`, `spent_usd`,
`first_cost_usd`, `requests` and `cache_hits`. It also gives:

- `ceiling_usd`, the pass's ceiling;
- `ended`, which is `false` for a pass that was killed before it ended;
- `per_unit`, the cost and time per unit.

`per_unit` covers the units that called a model in that pass. An answer from the cache counts. A
held unit, or one the pass never started, does not. `units` says how many there were. For each of
`spent_usd`, `first_cost_usd`, `requests` and `seconds`, it gives the `total`, the `median` and
the 90th percentile (`p90`).

| Exit | When |
| --- | --- |
| 0 | The report is on stdout. |
| 2 | An unknown kind, a folder that holds no task, or a task that a pass stopped in the middle of a write. One line on stderr says which. |
| 3 | A pass's write kept the read waiting, and `message` says so. Run the command again. |

A task that a pass stopped in the middle of a write is fixed by the next `pass start` of the kind.
That pass also runs every unit not yet answered, so it can spend up to its `--ceiling-usd`.

## Send the hard units to a stronger model

After a pass, you can run only the units that did not settle, on a stronger model. Name the role
and the model with `--model`:

```bash
seqforge pass start <kind> --ceiling-usd <dollars> --only unsettled,refused --model writer=<provider>/<name>
```

This needs the gateway and the stores, as any pass does.

A unit that a person's review holds counts with the review's ending, as long as its inputs have
not changed since the review. So `--only unsettled` leaves out a unit that a person has settled.
liulab-llms reads a task's reviews from the `reviews/` folder inside the task folder. No seqforge
command writes there. The folder holds only the reviews saved with liulab-llms's own
`Reviews.save`, and a task with no such folder has no reviews.
