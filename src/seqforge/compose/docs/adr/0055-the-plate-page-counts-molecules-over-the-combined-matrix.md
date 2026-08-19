# 55. The plate page counts molecules over the combined matrix, and disagrees with `X` on purpose

A plate cell's `n_umis` and `genes_detected` are counted over `umi_combined`, while the object's `X`
is the exonic UMI matrix and `uns["primary_matrix"]` says so — so a reader who opens the h5ad and
sums `X` by row gets a smaller number than the page printed for that cell. Counting them over `X`
instead was the alternative, and it buys that agreement at a price the page cannot pay back:
`saturation` sits in the same row, is one minus molecules over the UMI-carrying gene-assigned
fragments, and takes its molecule term from `umi_combined`, so an exonic total beside it would put
two populations on one row and the first reader to divide them would get a number meaning nothing.
SMART-seq3 is full-length besides, so an exonic total understates the yield these columns exist to
report. The disagreement is real and permanent, and the labels carry the qualifier — "UMI
(combined)", "Genes (combined)" — precisely so the page never claims to be reporting `X`. Undoing it
is expensive on both sides: a stamped `obs` column on every object written since, so a reversal
moves every plate figure and re-keys every compile.
