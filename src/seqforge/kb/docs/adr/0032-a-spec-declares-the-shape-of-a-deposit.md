# 32. A spec declares the shape of a deposit, and compose acts on that declaration without the manifest recording the outcome

Deriving "one sample is one cell" from the read layout is wrong in both directions at once —
SMART-seq2 has neither UMI nor barcode and is still one cell per file, UMI-tagged bulk has a UMI and
no barcode — and archive cardinality is the submitter's choice, so a `spec.yaml` declares it
instead. Dropping a starved cell where the samples are listed would make `dataset_hash` a function
of a knowledge-base number and break every recipe pinned to the dataset, and freezing the verdict
into the write-once manifest instead means a second compile re-reads a stale opinion of a floor that
has since moved — a cache with no key. So the threshold is applied at every compile under the live
knowledge base, the manifest and `units.tsv` deliberately disagree about what exists, and a deposit
with no cell above the floor refuses rather than shipping an empty `rule all` at exit 0.
