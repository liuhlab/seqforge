# 53. A mate that did not align is counted, not refused, and its count is derived twice

The chimera split asserted each output kept as many first mates as second. Where only one mate
aligns, the aligner omits the other and the mapped survivor is kept unbalanced; a pilot lost sixteen
of sixteen matrices to that refusal. The survivor carries the mate-unmapped flag, so it is a
**singleton**: each side's come off that side, and the PAIRED REMAINDER must balance. Rejected:
requiring both mates (discards evidence); per-template state (no buffer may be bought here); an
informational check (the only net under a halved output). The count is also derived a second,
independent way, which refuses a BAM whose aligner never wrote the dead records.

**Amended 2026-08-18 (#440): the second derivation is a BOUND, not an equality.** STAR writes a dead
mate's `RNAME` as `*` and its partner's chromosome in `RNEXT`, so the mate POINTER names the
Component; the first check read `RNAME` and counted zero against 5440 survivors. Corrected, the
equality still refused healthy cells, per Component (4929/511 by survivors, 4928/512 by pointers,
all 90 disagreeing fragments multiply placed) and in total (33026 against 33027, a three-locus pair
leaving a placeless copy). One fact: **a per-fragment correspondence between ends does not survive
multi-locus emission**, since one representative of a locus set is written, and the dead record's
zero hit count makes filtering stateful. So `pointers >= survivors` is ASSERTED; the per-Component
pair, whose gap bounds half-mapped fragments spanning two organisms, and the excess are REPORTED.

**Amended 2026-08-21 (#451): the bound is two-sided, and only a UNIQUELY placed survivor is owed
anything.** The mirror occurs: three cells of a 784-cell plate came up one short, each a multiply
placed survivor whose dead mate is in no file. An emitted locus leaves a dead half no survivor is
owed; an unemitted one takes its dead half with it. So `survivors - pointers <= multiply-placed
survivors` is ASSERTED, and both signs of the difference are REPORTED as two non-negative counts.
Rejected: an informational check, since zero pointers against survivors no multiply-placed count
covers is the flag this exists to catch; a fixed tolerance, invented at review against a deficit
that scales with the plate. The bound cannot attribute: a uniquely placed survivor's missing mate is
forgiven up to that many, and separating them needs state this module may not hold.
