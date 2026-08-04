# 26. An alert is advisory — the first backward edge writes nothing and changes no exit code

Date: 2026-08-04

## Status

Accepted.

## Context

Everything in this compiler flows one way. Bytes and prose become a **Manifest**, a manifest and a
**Recipe** become a **Compiled pipeline**, and the user submits it. Each stage reads what the stage
before it wrote and nothing reads back — which is why a manifest can be immutable, why a hash can
name a pairing, and why "what did the compiler decide" has one answer at any moment.

The cross-check breaks that shape. `seqforge report` is the first place the compiler holds both
halves at once — what it decided, and what came back — and an **Alert** is what joining them
produces: a threshold comparison over the **Metric**s a finished pipeline wrote, naming the decision
it implicates and the value that decision currently carries.

**The obvious reading is that it should act on what it found**, and a future reader will reach it
from exactly the evidence the feature ships with. The alert has already resolved the decision to a
field and read its current value; where the alternative is enumerable it also carries what to set it
to. Writing it is the same code path with a `write_text` on the end, and it is what a user staring at
0.076% valid barcodes appears to want. The exit code reads the same way: "the compile succeeded and
the run is probably wrong" is precisely the situation an exit code seems to exist for, and there is a
`Warning` type sitting right there with the correct disposition already on it.

Both are unsound, for reasons that are not visible from the alert's own shape. This record is why a
reader who reaches them again does not have to re-derive that.

## Decision

**An alert points; it never moves what it points at.**

- **It writes no artifact.** Nothing on the alert path opens a `manifest.yaml` or a
  `processing.yaml` for writing, and it proposes no third file that stands in for either.
- **It changes no exit code.** No verb's code becomes a function of whether an alert fired.
- **It produces no refusal.** An alert is never a `Blocker`, a `Conflict` or a `Question`, and never
  turns a compile that succeeded into one that did not.

The dataset manifest is immutable and content-addressed ([0004](0004-two-artifacts-not-one.md)) and a
pairing's identity is hashed at compile time ([0005](0005-run-id-is-the-pairing.md)). Evidence
arriving *after* both may inform the user; it may not silently move either artifact. **The user
decides whether to recompose.**

## Why not recompose automatically

1. **The evidence is weaker than the act.** A threshold comparison establishes that a number is out
   of range. It does not establish *which* of the decisions it implicates is the wrong one — the
   chemistry rule implicates two, the chemistry call and the barcode read's role assignment, and a
   rule that cannot separate them cannot choose the edit. Naming a replacement where the alternative
   is enumerable is exactly as far as this evidence reaches, and it is what an alert already does.

2. **It inverts the direction of authority.** Editing `library.chemistry` moves `dataset_hash`, which
   is the identity of what the data *is* — decided from bytes, with prose able only to disagree
   loudly ([0010](0010-two-resolvers-one-blocks-one-warns.md)). An aligner's summary is neither bytes
   nor prose; it is the output of a decision, and letting it rewrite its own input makes the dataset's
   identity a function of the last pipeline anyone happened to run over it.

3. **Behind a flag is the same write.** `--fix` is not a weaker form of this. It is the same write
   with a consent step, and consent does not make a write-once artifact mutable (R11). What a user who
   agrees actually wants is not an edited manifest but a *second* **Recipe** — plural and sparse by
   design, written by `processing new`, moving no `dataset_hash` and compiling to its own directory.
   That shape already exists; a mutating flag would be a worse one built beside it.

## Why not make an alert a `Warning`, or raise the exit code

The refusal vocabulary is already there and reusing it is free: `Warning` is the non-blocking
advisory that exits 0, and 3 and 4 already route "stop" and "ask" ([0013](0013-cli-is-a-machine-interface.md)).
Both are wrong, and in opposite directions.

`Warning`, `Blocker`, `Conflict` and `Question` are **compile-time** verdicts. They are produced while
deciding, they attach to the artifact being decided, and `validate` is what emits them. An alert
exists only because a compile already finished and a pipeline already ran, so filing one as a
`Warning` puts a judgement about the *run* inside the record of the *compile* — and the next
`validate` over that same manifest, which reads no pipeline output at all, would legitimately not
reproduce it. A vanishing warning is worse than none. `Conflict` fails harder: it blocks at exit 4
until a human confirms, and what it would be blocking is a manifest already written, already hashed,
and already the input to work in flight.

The exit code is the same argument from the caller's side. `seqforge report` exits on whether it
**rendered** — the dataset's own verdict rides in the page and in the stdout summary, deliberately not
smuggled into the code — and `seqforge run` goes further still, treating the render as a view whose
*failure* is a skipped stage rather than a failed run. A page that could raise the exit code would
mean a sweep across 10⁴ datasets starts failing on a threshold nobody submitted a job against: advice
breaking pipelines that produced usable data, which is the inverse of the feature. A machine consumer
needs the alert itself, with its stable id, in the verb's JSON — not a one-byte lossy summary of it.

## Why not write the correction into a third file

The compromise that appears to dodge the argument entirely: touch neither artifact, write a
`processing.suggested.yaml` beside the recipe, let the user diff it. There is even precedent —
`compose` writes `processing.lock.yaml`, because disk is state (R5).

The precedent is what refutes it. `processing.lock.yaml` records what a compile *used*: a transcript
of a decision already taken, by code that had the authority to take it. A suggested recipe is the
opposite — a file shaped like an input, sitting where inputs live, written by nothing with the
authority to decide anything. Neither a human nor an agent globbing the workspace can tell the two
apart by looking, and a recipe is precisely the file this project invites you to hand back to
`compose`. Since a **Recipe** is plural and cheap, the user who agrees writes one; the alert has
already given them the field and the value currently set, which is the entire content a suggestion
would have carried.

## So in code

**Read the decision an alert names; never write it.** Attribution resolves each implicated decision by
*reading* the manifest and the recipe the collector already holds, and that read is the whole of this
path's contact with them: do not open either for writing, do not emit a third file standing in for
either, and do not add an alert to a `Blocker`, `Conflict` or `Question` list a validator will later
read. No verb's exit code may become a function of whether an alert fired. A rule that wants something
changed wants a new **Recipe**, and the user is the one who writes it.

**Enforced by.** `test_an_alert_never_rewrites_the_manifest_or_the_recipe` (`tests/test_report.py`).

## Consequences

- The next step stays the user's, and the alert makes it concrete rather than automatic: the field in
  manifest or recipe vocabulary, the value currently set, and — where the alternative is enumerable —
  what to set it to. A second recipe over one dataset compiles to its own directory and shadows
  nothing ([0005](0005-run-id-is-the-pairing.md)).
- Alerts reach a machine through the report verb's JSON, carrying their stable ids. That is the
  channel, and the exit code is not one.
- **Alert** is a `CONTEXT.md` term, and its avoid-lines against **Conflict**, **Warning**, **Blocker**
  and **Question** are this record in one line. They exist because the compile-time vocabulary is what
  a reader reaches for first.
- **What the gate covers, and what it does not.** The named test drives the whole path over a
  workspace and asserts both artifacts are byte-identical afterwards and the exit code is unchanged —
  the mutation this record is about. It cannot see the *shapes* the rebuttals above rule out: a
  suggested-recipe sidecar, or a future verb taught to act on an alert, would each leave the manifest
  and the recipe untouched and pass. Noticing those needs a guard this tree does not have — one over
  every path the report opens for writing, asserting that set is exactly the report's own output — and
  none exists. Until one does, both are caught at review and nowhere else.
- Nothing here constrains what an alert may *say*. Whether a threshold is defensible is a review
  obligation, exactly as it already is for a **Metric**'s level
  ([0025](0025-the-module-that-writes-an-artifact-owns-reading-it.md)).
