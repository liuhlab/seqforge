# What harvest cost before its fan-out was planned, and why Pro is the default model

Measured **2026-08-01** and **2026-08-02**, and moved out of the harvest skill on **2026-11-26**. The
skill keeps what to run; the numbers and where they came from live here.

## One benchmark case, before and after the fan-out was planned

**Method.** The benchmark case `GSE126954`, graded with its harvest stage on `deepseek-v4-pro`,
before and after two changes: the case's transcript was narrowed to the samples
its package pins, and each sample's run aliases were collapsed into one document. The figures are
recorded in the case header, `evals/benchmark/GSE126954/expected.yaml`, and in the commits
`eaa580b`, `c164c1c` and `e5665b4`.

| | Before | After |
| --- | --- | --- |
| Model calls | 983 | 22 |
| Input tokens | 3,141,899 | 86,220 |
| Wall time | 300.6 s | 131.7 s |

The series carries 966 run records, and before the change each run record with prose was its own
call. One sample, which no graded slice can join to, carried 910 of them: 92% of the case's model
spend and 83% of the whole benchmark's.

**What it established.** A dataset's cost was not computed until it had been paid. That is why
`harvest extract --dry-run` prints the plan before any call, and why only an experiment record's
protocol paragraph is asked anything now.

## Pro against Flash as the default model

**Method.** `seqforge eval run` over `evals/benchmark`, at commit `ac11b44`, with the same prompt and
one trial per model. Recorded in commit `584472a`, which changed the default; the method for the same
day's ruler change is in [`ruler-changes-that-moved-no-grade.md`](ruler-changes-that-moved-no-grade.md).

| | `deepseek-v4-pro` | `deepseek-flash` |
| --- | --- | --- |
| Speed | 2.6× faster | baseline |
| Output tokens | 93,716 | 328,857 |
| Documents failed | 1 | 6 |
| Cases graded correct | 15 of 18 | 12 of 18 |

Most of Flash's extra output was `field_not_permitted_for_doc` rejections, claims the prompt never
asked of that document.

**What it could not establish.** Correctness was never the axis: code re-greps every quote whichever
model proposed it, so the model choice moves cost and speed. Each figure is one trial, and a single
`--llm` grade on this tier was shown to flip between repeats, so read the grade row as a draw.
