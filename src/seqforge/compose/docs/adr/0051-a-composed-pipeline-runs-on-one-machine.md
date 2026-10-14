# 51. A snakemake instance runs on one machine

Corpus scale comes from many instances at once, never from one instance spreading its jobs across
machines. Every memory decision rests on that, unstated while the emitted Snakefile named a cluster
profile. One machine makes a shared genome copy possible: one segment, loaded once, attached by every
mapping job, so N concurrent jobs cost one index instead of N. A fanned-out DAG, one instance's jobs
spread over machines as a Slurm executor would, satisfies every dependency edge and still loads a
copy per job, because an edge guarantees order, not co-location. It stays out of scope. Several
instances over one results directory, each with its own targets, are inside the design. An
instance's jobs are the only claimants on its machine's memory.

A mapping job's memory figure is therefore **self-sufficient**: it covers everything one job needs,
index included, as if alone. That keeps the sort cap at three quarters of the request; three quarters
of an incremental figure would be smaller than the sort. It also settles concurrency. N jobs sharing
a copy cost one index plus N working sets, which no per-job number expresses, so a memory-based
limit charges the index N times. A 784-sample compile enforcing the declared figure admitted two
samples on a 96 GB machine, turning ~6 hours into ~65, and had to cap by **cores**. Six instances
run that arithmetic once per machine; no figure moves.

Two prices, accepted. A container that walls off shared memory makes every job load privately with
no error: it costs speed, not correctness, and is the operator's setting. And two instances against
one assembly on one machine can hold two copies: the copy is keyed by the genome directory, so the
second's defensive clear marks the first's live copy, and the first's next mapping job makes a
second. Two shards of one plate on one machine pay that too. The worst case is two copies, what no
sharing costs. Rejected: detecting a fanned-out DAG and degrading to private copies, since STAR
reports nothing that tells them apart. Rejected: correcting the emitted run instruction rather than
deleting it, since submission is the operator's business and guidance in a generated file drifts.

**Status.** Amended: the unit narrowed from a run to a snakemake instance. Every consequence above
holds, per instance.
