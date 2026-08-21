# 56. An aligner's per-assembly bound is a genome-table fact, copied into the recipe

At STAR's default `--alignIntronMax 0` there is no intron-length check at all, so what bounds a
novel intron is the contig; the lab works in compact genomes, and the worm plate carries `N` gaps to
1,049,334 bp on an organism whose longest annotated intron is 100,912 bp (#459). The bound is a fact
about the ASSEMBLY, and the lab already has one place for those — `liulab-genome`'s shipped table,
which the processing policy reads for the recipe's `ncbi_taxid`. So it is read from there too, and
then **written into `processing.yaml` rather than looked up by a rule**. That copy is the decision:
`run_id` folds the dataset, the recipe, the KB and the workflow and no pin on `liulab-genome`, so a
value read at run time would let one edited table cell change how a dataset aligns while two
compiled pipelines kept a single identity — the failure `compose.chimera` already refuses for module
dispatch, arriving as alignment instead. A chimera takes the maximum over its components, derived
from the name: one value serves the whole run because the aligner cannot bound a gap per contig, and
the two errors are not symmetric — too tight destroys real biology on the component with the longest
introns, too loose merely fails to help on the others. On an intron-free component the bound cannot
bind at all, which is the plainest demonstration that it is the backstop and the anchor filters are
the fix.

**Deriving the number from the annotation was rejected**, and it is the obvious reading: a GTF is a
catalogue of transcripts someone observed, so its longest intron is a floor on what biology does and
never a ceiling. Computing a ceiling from a floor is category-incorrect however carefully done, it
fails silently in the tight direction when it is computed wrong, and it would make the bound a
function of which annotation release happens to be registered. A seqforge-side table lost for the
older reason: genome facts are `liulab-genome`'s, and a second place to look is a parallel universe.
Two prices, accepted. seqforge now reads a genome-table column that is not a cross-reference, which
is the upstream half of this change and has its own record there. And an assembly the table does not
list emits no flag at all — read with `.get`, so the composer never owes the key — because an
unfilled row must change nothing rather than impose a number nobody chose.

**Status.** Introduced by #464 under the plan in #461. The values and their rationale live upstream,
one row per assembly; revisiting one is a table edit and nothing here moves.
