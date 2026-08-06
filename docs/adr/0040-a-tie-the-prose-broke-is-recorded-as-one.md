# 40. A tie the prose broke is recorded as one, and correcting that moves `dataset_hash`

Date: 2026-08-05

## Status

Accepted. Closes a defect in [ADR-0010](0010-two-resolvers-one-blocks-one-warns.md)'s recording, not
in its precedence, and narrows what [ADR-0020](0020-a-family-term-narrows-it-does-not-conflict.md)
implies about provenance. Found by the 2026-08-05 audit of the agent-facing tree.

## Context

R9 says: record which rung resolved each field. For one branch it did not.

When the bytes tie across a processing-divergent pair, `escalate` may break the tie from a
span-verified assertion. That branch returned `rung_reached=max(rung, 0)`, where `rung` is the
greatest rung any tied member reached — 2 or 3. **`max(rung, 0)` is a no-op over a non-negative
rung**, so the recorded value was the tie's *byte* rung, every time. `manifest fill` then stamped
`basis="observed"` on the chemistry envelope unconditionally, and the branch raised nothing of its
own, because a hypothesis that names the picked candidate *agrees* with it and `_detect_conflicts`
finds no disagreement to surface.

So a chemistry chosen by prose and a chemistry chosen by bytes arrived in the manifest identical: same
basis, same rung, no record either way. The artifact said the bytes had settled a question the bytes
had just failed — the definition of a divergent tie being that they separate nothing.

The code was already arguing with itself. `_metadata_disambiguation`'s docstring has read *"pick it
(rung 0, surfaced `asserted`)"* since it was written. The docstring was right and the function it
described was not, which is why this is recorded as a defect rather than a change of mind.

Nothing held it. `test_escalate_metadata_disambiguates_divergent_tie` asserted the winner and the
absence of a question, and neither the rung nor the basis — the two things that were wrong.

## Decision

**The rung recorded for a field is the ladder step that settled it, and where prose settled it the
basis says so.**

| the hypothesis | who chose the leaf | `rung_reached` | envelope basis | raises |
| --- | --- | --- | --- | --- |
| **names the winner** | the prose; the bytes tied and separated nothing | `0` | `asserted` | a `resolved` `Conflict` naming the assertion and the score both members tied on |
| **names the winner's family** | the bytes, inside a field the prose narrowed | the tie's byte rung | `observed` | nothing new |

`_metadata_disambiguation` returns which of the two happened; the caller records them differently and
must not merge them.

**This moves `dataset_hash`** for any dataset whose chemistry was settled by a named assertion. Both
fields live in `LibrarySection.chemistry`, and `library` is one of the two sections the hash covers.

## Why the family case keeps the byte rung

The first implementation credited both paths to the prose, and a test refused it: the within-family
case (`test_metadata_v3_vs_reads_v2_also_resolves_at_the_leaf`) began raising two resolved conflicts
where the tree expects one.

That is [ADR-0020](0020-a-family-term-narrows-it-does-not-conflict.md) defending itself. A family
term is *deliberately vague about the leaf* — that record exists to say an ancestor of the observed
leaf is agreement and not a competing claim. Recording the leaf as `asserted` at rung 0 would credit
an assertion with a choice it explicitly declined to make, and the second conflict was the same
narrowing counted twice. The prose eliminated the non-members; the bytes chose inside what was left,
and that is an observed decision reached through a smaller field.

## Why this is not an `observed` ↔ `asserted` conflict, and does not block

ADR-0010 blocks at exit 4 when an observed value contradicts an asserted one, and a reader may expect
that here, since an assertion is deciding a chemistry.

It does not apply, for a reason worth stating once: **there is no observed value to contradict.** A
processing-divergent tie is the bytes returning two positions with nothing between them. The
assertion is the only position with content, nothing is overridden, and no human answer is needed —
which is the test ADR-0010 uses for blocking in the first place. The `resolved` status is the same
auditable, non-blocking channel `_inherited_conflict` already uses: it surfaces in the report, moves
no exit code, and asks nobody anything.

## Why not leave the basis `observed` and correct only the rung

The smaller fix moves one number and no meaning: `rung: 0` beside `basis: observed` would at least
stop claiming the bytes reached the answer. It was rejected because it produces a state the
vocabulary cannot express. `observed` is defined as *a value read out of the bytes* and is the
authority nothing asserted overrides; rung 0 is *metadata*. A field carrying both says it was read
from bytes at the rung where bytes are not consulted, and a reader reconciling the two has to know
this branch exists to know which half to believe. Half a correction is a new inconsistency.

## Why the hash moving is acceptable

[ADR-0004](0004-two-artifacts-not-one.md) fixes `dataset_hash` against a **change of intent** — the
whole point of the two-artifact split is that recompiling with a different recipe cannot move it.
This is not intent. It is the correction of a value that was wrong, and a content address over a
corrected fact *should* differ from one over the error.

The alternative is to exclude provenance-shaped fields from the hash so corrections are free, and
that is a worse trade in the direction this compiler cares about: the basis and rung are how a
consumer of the corpus decides how much to trust a chemistry, so a hash that ignores them would call
two manifests identical when they disagree about who decided. The blast radius is small and visible —
only divergent ties broken by a named assertion, which no shipped fixture produces, which is itself
why the defect survived.

## So in code

**Record the rung that settled a field, and let the basis follow the rung.** When you add a branch
that decides a field from something other than the bytes, ask what rung that source is and stamp it;
if the answer is not `observed`, `manifest fill` must not say `observed`. The failure mode is silent
by construction — an artifact that records the wrong decider still validates, still hashes, and reads
as ordinary.

**And a no-op guard is worse than no guard.** `max(rung, 0)` looked like a floor and was
unconditionally the identity, so the intent was visible in the source and absent from the behaviour
for as long as the branch existed. Where a clamp can never fire, delete it or assert it.

**Enforced by.** `test_escalate_metadata_disambiguates_divergent_tie` (`tests/test_resolve.py`)
asserts the rung, the basis on the resolution, the two positions and that exactly one `resolved`
conflict is raised; `test_metadata_v3_vs_reads_v2_also_resolves_at_the_leaf` (same file) holds the
other half — the family path keeps a byte rung and raises no second record. Nothing asserts the
`asserted` basis on a *filled manifest*, because no fixture reaches this branch through `manifest
fill`; that gap is the reason the defect lived, and closing it needs a fixture whose bytes tie across
a divergent pair, which the KB's shipped specs do not currently offer.

## Consequences

- **A manifest filled before this change records the wrong decider for an affected dataset, and
  re-filling it changes its `dataset_hash`.** Nothing migrates it; the corpus is regenerable and the
  old value is not worth preserving, since what it recorded was false.
- **`rung: 0` is now readable as a claim.** It means prose settled the chemistry, and it is the only
  rung that means that. Any other rung on a chemistry means the bytes chose.
- **The report gains a `resolved` conflict on affected datasets.** It moves no exit code, so a caller
  branching on exit status is unaffected; a caller counting conflicts sees one more.
- **The family path is unchanged in every observable way**, which is deliberate: this record narrows
  what ADR-0020 implies about provenance without touching what it decided about matching.
