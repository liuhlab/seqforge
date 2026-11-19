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
  a person has already answered. Make the clone with `gh repo clone liuhlab/seqforge-review`.

The review commands also need the GitHub CLI, `gh`, logged in with `gh auth login`. They act on
GitHub as you, and need no more than the `repo` scope that login gives by default.

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

## File units for review

A unit that ends unsettled or refused waits in its kind's **review queue** until a person answers
it. People answer in the lab's private review repo, one GitHub issue for each unit:

```bash
seqforge review file <kind>
```

This files five units from the queue, in the kind's own review order. Each new issue asks a person
for a few minutes, so keep the number small. `--count` sets a different number.

A unit is never filed twice for the same inputs, whether its first issue is still open or closed.
When a unit's inputs change, a pass answers it again, and it can then be filed again. A unit whose
inputs changed after its latest pass waits: it is filed once a pass has answered the new inputs.

Each issue shows:

- the unit's records, with links
- each pass's answer, with the models that gave it and its stamp
- each check that the answer failed, with the check's message
- the boxes to tick: one answer to start from, which is a pass's answer or one that the kind's
  code offers; the kind's questions about single items; and the reasons to drop the unit

To answer, tick the boxes, then close the issue. Close it as completed to keep your answer, or as
not planned to drop the unit with the reasons you ticked. A hidden line at the top of the issue
holds the kind, the unit's key, its inputs and the answers you can start from. Do not edit it.

Each issue has the label `review:<kind>`, and a label `check:<name>` for each check that its
answer failed. Filter the issue list by a label to see one kind or one check together. The command
makes each label that the repo does not have yet.

The options:

| Option | What it does |
| --- | --- |
| `--count` | How many units to file. Default 5 |
| `--task DIR` | The task folder. Default: `seqforge/passes/<kind>/` in the current folder |
| `--reviews DIR` | The review repo's clone, if `SEQFORGE_REVIEWS` does not name it. The issues go to the GitHub repo that its `origin` names |

stdout carries one JSON object: `kind`, `task`, `filed`, which lists each filed unit's `key` and
the issue's `url`, and `unfiled`, the number of units in the queue that are still not filed.

| Exit | When |
| --- | --- |
| 0 | It filed what it could, up to `--count` |
| 2 | A setup it cannot use: an unknown kind, no review clone, no task in the folder, no `gh`, or a clone whose `origin` is not on GitHub. One line on stderr says which |
| 3 | Another process holds the task, such as a running pass, and `message` names it. Or a GitHub call failed: `stopped` is `github_failed`, and `filed` lists what was filed before it. Run the command again to file the rest |

## Read reviews back

Once people have closed issues, read their answers back:

```bash
seqforge review read <kind>
```

This reads each closed issue of the kind that has not been read back yet:

- An issue closed as completed stores the answer that its ticks make. The review records the GitHub
  login of the person who closed the issue, and the pass whose answer they started from.
- An issue closed as not planned stores a drop, with the reasons ticked as its note.

Each review goes into the kind's file in the `reviews/` folder of the clone. From then on the unit
counts as settled or dropped, and a pass leaves it out while its inputs stay the same. The command
does not open the task, so it works while a pass runs.

If the ticks make no answer, or the answer fails one of the kind's checks, nothing is stored. The
command adds a comment to the issue that says why, and reopens it. Fix the ticks, then close the
issue again.

If the unit's inputs changed after the issue was filed, its answer is for inputs that the unit no
longer has, so nothing is stored. The command says so in a comment, adds the label
`inputs-changed`, and leaves the issue closed. A pass answers the new inputs, and the unit is filed
again if it still needs a person.

Each issue is read back only once. After storing, the command commits the kind's review file, and
nothing else in the clone, then pushes it. If another clone pushed first, the command pulls, and
git's union merge keeps the lines from both. If the push still fails, the reviews stay committed in
the clone, and the next run pushes them.

The options:

| Option | What it does |
| --- | --- |
| `--reviews DIR` | The review repo's clone, if `SEQFORGE_REVIEWS` does not name it. The reviews are stored, committed and pushed there. The issues are read from the GitHub repo that its `origin` names |

stdout carries one JSON object:

| Field | Value |
| --- | --- |
| `kind` | The kind |
| `answered` | Each stored answer: the unit's `key`, the issue's `url`, `who` closed it, and `accepted_pass`, the pass whose answer they started from, or `null` for an answer the kind's code offered |
| `dropped` | Each stored drop: `key`, `url`, `who` and `note` |
| `reopened` | Each reopened issue: `key`, `url`, and the `reason` that its comment gives |
| `inputs_changed` | Each issue left closed because its unit's inputs changed: `key` and `url` |
| `pushed` | `true` if it pushed, `false` if the push failed, or `null` if there was nothing to push |
| `stopped`, `message` | Only on exit 3: `github_failed` or `git_failed`, and the message from GitHub or git |

| Exit | When |
| --- | --- |
| 0 | It read back each closed issue that it could, and reopened none |
| 2 | A setup it cannot use: an unknown kind, no review clone, no `gh`, a clone whose `origin` is not on GitHub, or a kind that reads liulab-data's store when the store cannot be opened. One line on stderr says which |
| 3 | A GitHub call or a git step failed. The reviews stored before it are committed in the clone. Run the command again to read the rest and push. Exit 3 wins over exit 4 |
| 4 | It reopened at least one issue, so a person has to answer it again |

## Set up the review repo

The lab set up the review repo once, with `gh`. These are the steps, for the record or for a new
copy:

```bash
gh repo create liuhlab/seqforge-review --private \
  --description "Reviews of seqforge units: one issue per unit, answered by ticking boxes and closing"
gh repo clone liuhlab/seqforge-review
cd seqforge-review
mkdir -p reviews
printf '*.jsonl merge=union\n' > reviews/.gitattributes
# Write a short README.md that says what the repo is for, then:
git add README.md reviews/.gitattributes
git commit -m "Set up the review repo"
git branch -M main
git push -u origin main
gh label create hand-check --repo liuhlab/seqforge-review \
  --description "The benchmark's hand-check of a settled unit"
```

The `reviews/` folder holds one review file for each kind, one line for each review. Each person's
clone adds lines to the same files, so `merge=union` tells git to keep the lines from both sides of
a merge, not to stop on a conflict. The `hand-check` label is for the benchmark's own check of
settled units. The review commands make every other label when they first need it.
