# 56. An aligner's per-assembly bound is a genome-table fact, copied into the recipe

At STAR's default `--alignIntronMax 0` there is no intron-length check at all, so what bounds a novel
intron is the contig, and on a compact genome the aligner buys score with junctions carrying no
information (#459). That bound is a fact about the ASSEMBLY, and the lab has one place for those —
`liulab-genome`'s shipped table, which the processing policy already reads for the recipe's
`ncbi_taxid` — so it is read from there and then **written into `processing.yaml` rather than looked
up by a rule**. The copy is the decision: `run_id` folds no pin on `liulab-genome`, so a value read at
run time would let one edited table cell change how a dataset aligns while two compiled pipelines
kept a single identity. A chimera takes the maximum over its components, because the aligner cannot
bound a gap per contig and the two errors are not symmetric — too tight destroys real biology on the
component with the longest introns, too loose merely fails to help on the others. On an intron-free
component it cannot bind at all, which is the plainest demonstration that this bound is the backstop
and the anchor filters are the fix.

**Deriving the number from the annotation was rejected**, and that is the obvious reading: a GTF
catalogues transcripts someone observed, so its longest intron is a floor on what biology does and
never a ceiling; computing one from the other fails silently in the tight direction, and it would
make the bound a function of which release happens to be registered. Two prices, accepted. seqforge
reads a genome-table column that is not a cross-reference, which is the upstream half and has its own
record there. And an unlisted assembly emits no flag at all — read with `.get`, so the composer never
owes the key — because an unfilled row must change nothing rather than impose a number nobody chose.

**Status.** Introduced by #464 under the plan in #461. The values live upstream, one row per
assembly; revisiting one is a table edit and nothing here moves.
