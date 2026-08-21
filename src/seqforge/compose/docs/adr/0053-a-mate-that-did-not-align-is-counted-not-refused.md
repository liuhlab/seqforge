# 53. A mate that did not align is counted, not refused, and its count is derived twice

The chimera split ended every run by asserting each output had kept as many first mates as second,
because both mates of a template carry one hit count and one chromosome. Both premises are true and
the conclusion is not: where only one mate aligns the aligner omits the other, the survivor is a
mapped primary alignment and is kept, and nothing is in the file to balance it. A pilot lost sixteen
of sixteen matrices to that refusal, the arithmetic closing exactly and the rarer, more soft-clipped
organism hit hardest. Such a survivor carries the mate-unmapped flag, so it is a **singleton** by
construction: each side's own come off that side, and the PAIRED REMAINDER is what must balance.
Requiring both mates lost because it discards real evidence; per-template state, because the flag
test is per record and no buffer may be bought here; an informational check, because it is the only
net under a halved output. A dead mate now sits at its live mate's coordinates, so the count is
derived a second, independent way and the two compared — stronger than what it replaces, still
nothing held, and it refuses a BAM whose aligner never wrote those records, saying so.

**Amended 2026-08-18 (#440): the second derivation is a BOUND, not an equality.** Two things were
written here without having been watched, and a pilot re-run falsified both. A dead mate does not
sit at its live mate's coordinates — STAR writes `RNAME` as `*` and the partner's chromosome in
`RNEXT`, so the mate POINTER is the field that names a Component, and the first version of the check
read the wrong one and counted zero against 5440 flagged survivors. Corrected, the equality still
refused healthy cells: per Component (5440 either way, 4929/511 by survivors and 4928/512 by
pointers, all 90 disagreeing fragments multiply placed), and then in total (33026 survivors against
33027 pointers, the extra a fully mapped three-locus pair that also left a placeless copy of one
mate behind). Both are one fact, and it is the general one: **a per-fragment correspondence between
the two ends does not survive multi-locus emission** — one representative of a locus set is
written, so another member's dead half can be left with no survivor to answer it and can point at an
organism the emitted alignment did not take. Filtering the multiply-placed out is not available statelessly,
since the dead record's own hit count is zero. What is derivable is that every survivor is owed
exactly one placeless record, so what is ASSERTED is `pointers >= survivors`; what is REPORTED is
the per-Component pair, whose gap bounds the half-mapped fragments spanning two organisms, and the
excess, which counts the placeless records belonging to fragments that did align.

**Amended 2026-08-21 (#451): the bound is two-sided, and only a UNIQUELY placed survivor is owed
anything.** "Every survivor is owed exactly one placeless record" reads as an entailment from the
flag and is not one; the same emission policy that produced the excess above produces its mirror,
and three cells of a 784-cell plate came up exactly one short — each shortfall a survivor whose own
hit count was above one and whose dead mate is in no file, against 52366 placeless records the
aligner plainly did write, so the refusal's stated diagnosis was false as well as its premise. A
locus that WAS emitted leaves a dead half no survivor is owed; a locus that was NOT emitted takes its
dead half with it. So what is ASSERTED is `survivors - pointers <= multiply-placed survivors`,
one more streamed counter, and both signs of the difference are REPORTED — as two non-negative
counts, because at most one can be non-zero and every reader of these payloads, including those
already on disk, takes a count as non-negative. Demoting the check to informational lost: zero
pointers against a survivor population no multiply-placed count can cover is still the aligner flag
this exists to catch. A fixed tolerance lost twice over: it is a number invented at review that
nobody can read afterwards, and the deficit is a population that scales with the plate, not a
constant. What the bound cannot do is attribute — in a total, a uniquely placed survivor's missing
dead mate is forgiven up to that many — and separating them needs the dead record's own hit count,
which is zero, or per-template state, which this module may not hold.
