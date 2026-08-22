# 51. A snakemake instance runs on one machine

Corpus scale comes from many instances at once, never from one instance spreading its jobs across
machines — the assumption every memory decision here rests on, stated nowhere until now while the
emitted Snakefile suggested the opposite by naming a cluster profile. One machine is what makes a
shared genome copy possible at all: one segment, loaded once, attached by every mapping job, so N
concurrent jobs cost one index instead of N. A fanned-out DAG — one instance's jobs spread over
several machines, which is what a Slurm executor would do — satisfies every dependency edge and
still loads a private copy per job, because an edge guarantees ORDER and never co-location, and it
stays OUT of scope. What this permits instead is a plate run as several instances over one results
directory, each given its own targets, which is ordinary practice and is now inside the recorded
design rather than an undocumented exception to it. Allocation of the whole machine comes with it —
the jobs of one instance are the only claimants on the memory that instance was given.

A mapping job's memory figure is therefore **self-sufficient**: it covers everything that one job
needs, index included, as though it were the only job on the machine. That is what keeps the sort cap
at three quarters of the request, since three quarters of an *incremental* figure would be smaller
than the sort it is meant to cover. It also settles what limits concurrency. N jobs sharing a copy
cost one index plus N working sets, a sum no per-job number can express, so a memory-based limit
charges the index N times and under-admits every time — which is how a 784-sample compile enforcing
the declared figure admitted two samples on a 96 GB machine, turning a ~6 hour run into a ~65 hour
one, and had to cap by **cores** instead. Splitting a plate across six instances runs that
arithmetic six times, once per machine; no figure in it moves.

Two prices, both accepted. A container that walls off shared memory makes every job load privately
with no error anywhere; the cost is speed and never correctness, and the setting belongs to the
operator submitting the pipeline rather than to a file we generate. And two instances against one
assembly on one machine can hold two copies rather than one — the copy is keyed by the genome
directory, so the second instance's defensive clear marks the first's live copy and the first's next
mapping job creates a second instead of attaching. Two shards of one plate sharing a machine now pay
that same price and not only two unrelated runs, which is what the narrowing costs. The worst case
is two copies, which is exactly what happens today with no sharing at all. Detecting a fanned-out
DAG and degrading to private copies lost: STAR reports nothing that distinguishes the two, so the
check could never fire. Correcting the emitted run instruction rather than deleting it lost for a
plainer reason — how a pipeline is submitted is the operator's business, and guidance baked into a
generated file drifts from practice.

**Status.** Amended — the unit narrowed from a run to a snakemake instance. Every consequence drawn
above survives unchanged, now per instance.
