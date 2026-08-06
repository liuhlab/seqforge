# 23. STAR's memory escalates on retry, and a job that still does not fit fails loudly

STAR's per-read Solo array is unbounded in the input and no `--limit*` flag bounds it, so a big
sample is killed from outside with a log indistinguishable from a preempted node; tripling the
request over three attempts turns the common overrun into a retry and the residue into STAR's own
refusal, which names the bytes it wanted. Sizing every job for the worst sample instead would charge
~36 GB of headroom to every dataset forever, when a per-recipe `mem_gb` override already expresses
intent about one dataset — and no default is provably enough against a corpus that keeps growing.
Both the request and the cap STAR is handed are `resources:` entries because snakemake re-expands
`resources` on retry and never `params`, so the `params:` spelling freezes at attempt 1 while
passing every structural check.
