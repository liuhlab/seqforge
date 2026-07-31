# 17. One type checker, and the editor runs it

Date: 2026-07-31

## Status

Accepted. Supersedes the strict/non-strict package split — the hand-maintained list of in-scope
packages in the `typecheck` task, and the `follow_imports = "silent"` that existed only to let the
packages outside it be imported at a lower standard.

## Context

Open this repo in VS Code and Pylance paints errors through most of the tree — 125 of them, in
`standard` mode, against the pixi `default` environment. `pixi run check` is green at the same
moment. A developer therefore has two checkers telling them two different things about the same file,
and no way to know which one CI will believe without running it.

Neither tool is wrong. `mypy --strict` was scoped by hand to eight packages; Pylance reads
everything. Three-quarters of what Pylance reported (95 of 125) was in `tests/`, which mypy had never
looked at. The rest was in the packages the scope deliberately omitted — `cli/`, `io/`, `kb/`,
`hooks/`.

**The obvious reading is that the warnings are cosmetic and should be configured away.** A reader
arriving at 125 standing errors, a green pipeline and zero user-visible bugs will reach that reading
from the same evidence, which is why this file exists: so they do not have to re-derive it, and so
they know what reading the noise actually bought.

It bought two things, and both cost real time:

- **The editor's signal was unusable.** 125 standing errors mean a genuinely new one is invisible.
  The IDE's fast feedback loop — the thing that catches a type error before you run anything — had
  been switched off by noise rather than by choice.
- **A fifth of the noise was a real design defect the project could not see.** 49 findings were
  container invariance: parameters typed `list[X]` where the function only iterates, forcing every
  caller to build exactly the wrong list. One signature — the fingerprint builder's `files` —
  accounted for about twenty call sites that had to construct `list[str | Path]` in order to pass a
  list of paths. mypy would have said so years earlier in any package it had been pointed at.

Widening the whole scope surfaced 226 findings across the four trees. **None of them was a runtime
defect.** What they located was a fifth of the codebase forcing callers into the wrong container
type, four different spellings of one validation boundary, a package docstring describing a consumer
that does not exist, and a glossary violation the naming had hidden.

## Decision

**mypy is the only type checker, its scope is the whole repository, and the editor runs the same
binary CI runs.**

| | before | after |
| --- | --- | --- |
| gate | `mypy` + a hand-written list of eight packages in the task string | `mypy`, scope declared in `[tool.mypy] files` |
| scope | `models/ probe/ resolve/ manifest/ compose/ workflows/ harvest/ evals/` | `src/ tests/ scripts/ skills/` |
| tiers | strict set vs. everything else, plus `follow_imports = "silent"` | uniform strict, no tiers; only third-party stub declarations remain |
| editor | Pylance `standard`, disagreeing with CI | Pylance's checker `off`; the mypy language server, on the pixi binary |
| holds it open | nothing | `test_nothing_tracked_escapes_the_type_checker` |

The scope moves **out of the task string and into the project file**, because the task string is
reachable only by things that run the task — the concurrent gate runner, the pre-commit hook and CI.
An editor extension reads the config. Putting the scope where all four look is what makes "the
editor's errors are CI's errors" true by construction rather than by convention.

`scripts/` and `skills/` are in scope because both already passed at full strictness with zero
errors. Covering them cost nothing and closed the hole where a new top-level package would have gone
silently unchecked.

## Why not two checkers in CI

The version of this decision that keeps both is: add pyright to the gate, fix what each reports, and
enjoy two opinions. It was rejected on the **dual suppression tax**, which is permanent rather than
one-off.

1. Every deliberate suppression needs two spellings. The manifest policy module already carried a
   mypy `type: ignore`; pyright would want a `pyright: ignore` beside it, forever, and the pair would
   drift the first time one checker's opinion moved.
2. `[[tool.mypy.overrides]]` has no per-module twin in pyright's config model with the same shape.
   The scipy and PDF-backend blocks — which exist because those packages ship no usable type
   information — would have to be re-expressed as something else, or the errors they suppress would
   have to be suppressed inline instead, which is the tax again in another place.
3. Two checkers is two answers to "is this green?", which is the problem this record is about. Adding
   a second gate to fix a two-checker disagreement is not a fix.

`basedpyright`, `ty` and `pyrefly` lose for the same reason: the cost is not which checker, it is how
many.

## Why not keep the strict/non-strict split

The split was defended as "not taste: a wrong type in the strict set poisons the corpus." That is a
true statement about consequences and a bad basis for a scope. `cli/` is the surface a user actually
types into; `io/` is where a remote read is bounded; `kb/` holds the specs every decision is scored
against. A wrong type in any of them reaches the corpus through one more frame, not through none.

The split also had a running cost the argument never priced: **"which tier is this file in?" is a
question every contributor has to hold**, and `follow_imports = "silent"` meant a module inside the
strict set was checked against the *silenced* types of a module outside it. The strictness was
therefore not what it said it was, in exactly the packages the split claimed to be protecting.

Measured, the split saved nothing worth having: mypy over all four trees runs in about 5 seconds.

## Why not just turn the noise off and keep two checkers

Setting Pylance to `off` without the sweep would have produced a quiet editor and a green pipeline in
an afternoon. It was rejected because the 226 findings had already been read by then, and 49 of them
were a live design defect with about twenty call sites. Silencing a checker that has just located a
real problem is how the problem becomes permanent.

The sweep is therefore the price of the config change, and it landed with it: the repo is never in a
state where the config claims a scope that does not pass.

## Why there is no `implicit_reexport` tier, though the plan called for one

This decision was drafted with one deliberate exception: turn `implicit_reexport` back on for the
test package. Twenty findings were tests reaching a source module's `time`, `requests` or `urllib`
attribute in order to patch it, and the argument writes itself — `no_implicit_reexport` guards a
public-API boundary, and between a test and the module it tests there is no such boundary.

**That override cannot be written**, for a reason not visible from the error message. mypy reports
the finding at the test — `tests/test_remote.py:548: Module "seqforge.io.remote" does not explicitly
export attribute "requests"` — but evaluates the flag against the module doing the **exporting**.
Measured on the nine findings in that one file:

| `implicit_reexport = true` on | findings cleared |
| --- | --- |
| the test module (`test_*`) | 0 of 9 |
| `seqforge.*` | 9 of 9 |
| the four patched modules, by name | 9 of 9 |

So the real choice was never "tests or not tests"; it was how much of `src/` gives up the guard to
accommodate a test. Both surviving options put the exception on production modules to fix a problem
no production module has.

The third option is that the tests do not need the exception. `remote.requests` **is**
`sys.modules["requests"]` — the attribute exists because `remote` wrote `import requests` — so
`monkeypatch.setattr(requests, "get", ...)` after a plain `import requests` in the test is the
identical mutation of the identical object. All twenty sites moved to that form, the guard stays on
for every module in the tree, and the configuration has no tier at all.

Note what was *not* done: no seam was injected into a retry path, no source module changed, and no
test changed what it asserts. The edit is which name the test reaches the module by.

## Why the guard derives its scope from `git ls-files`

The scope test has to tolerate the gitignored scratch harnesses developers keep in `tests/`
(`_*_test.py`, `_test_*.py`) while refusing to tolerate real code being hidden. The obvious
implementation — mirror the two glob patterns into `[tool.mypy] exclude` and assert the two lists
agree — is **a hand-maintained pair that must not drift**, which is the exact shape this repo has
already treated as a defect three times.

Deriving the first assertion from the tracked file list removes the pair. An untracked scratch
harness is never demanded by assertion 1, so nothing has to know its name; assertion 2 then confirms
that the exclusion permitting it to be skipped is legitimate, by checking the exclusion is gitignored
rather than by checking it against a copy of itself.

## So in code

**Do not narrow the checker's scope, and do not add a second checker.** A new top-level Python
package is type-checked the moment it is committed — you add nothing to make that happen, and a
`[tool.mypy] files` entry you remove will fail the suite rather than quietly reduce coverage. If a
file will not check, fix it or `git rm` it; adding it to `[tool.mypy] exclude` only works when it is
already gitignored, which means it is not code anybody ships. Deliberate suppressions stay inline,
carry a comment saying why they are deliberate, and are spelled for mypy alone — a `pyright: ignore`
in this tree is evidence somebody re-added a checker this record removed.

**Enforced by.** `test_nothing_tracked_escapes_the_type_checker` (`tests/test_repo_invariants.py`)
for the scope and the exclusion list, and the `typecheck` task for the errors themselves.

## Consequences

- **The editor and CI cannot disagree**, because there is one checker and both run it. A clean editor
  means a green typecheck — by construction, not by discipline.
- **Pylance keeps everything that is not a checker**: hover, completion, go-to-definition, symbol
  search and unresolved-import reporting. None of those duplicated mypy, and all of them remain.
- **The live squiggles depend on an extension the repo can only recommend.** `.vscode/extensions.json`
  suggests the mypy language server and `.vscode/settings.json` points it at the pixi environment's
  binary, but a developer may decline it. They then lose live feedback and fall back to running the
  task; nothing breaks, and the "editor shows what CI shows" property is simply unrealised for them.
- **No module in the tree is held to a lower standard than any other.** The one exception this
  decision was drafted with turned out to be unwritable and unnecessary, which is the section above.
  What remains in `[[tool.mypy.overrides]]` is only the declaration that three third-party packages
  ship no type information (scipy, the two PDF engines, pooch) — a fact about them, not a tier.
- **CI gains a few seconds inside an existing job.** Measured on the same box, 66 files to 138:
  **+3.8s** warm (1.4s to 5.2s) and **+2.1s** cold (17.3s to 19.4s). No new job, no new runner, no
  step reordering; the four-job shape and its 62-second wall clock stand, with `test` still the
  critical path at 51s and the lint job holding ~15s of slack against it. Splitting that job's three
  steps across runners was measured and rejected: three `setup-pixi` installs to save four seconds
  off a job that is not the pole.
- **Two documents were false the moment the scope moved** and were corrected in the same change: the
  typing section of [`docs/agents/toolchain.md`](../agents/toolchain.md), which enumerated the
  in-scope packages, and the `typecheck` comment in `pyproject.toml`, which said the same thing more
  briefly.
- **The scipy and PDF-backend overrides survive untouched.** They deliberately blind mypy to stubs
  pyright reads; with pyright's checker off, the disagreement they cause has no observer. Removing
  them is a separate question with an unmeasured error count behind it.
- This record supersedes the split but implies nothing about what the compiler decides. No manifest
  field, no scoring behaviour, no refusal and no exit code moved — in particular the closed
  instructable surface ([0011](0011-closed-instructable-surface.md)) and produce-every-answer
  ([0012](0012-produce-every-answer-rather-than-ask.md)) are untouched, and the sweep's one change
  near them was to stop three frames *upstream* of construction from claiming a closed vocabulary
  they never validated.
