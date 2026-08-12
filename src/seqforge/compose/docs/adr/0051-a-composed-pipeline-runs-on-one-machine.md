# 51. A composed pipeline runs on one machine

Corpus scale comes from many runs at once, never from one run spreading its jobs across machines —
the assumption every memory decision here rests on, stated nowhere until now while the emitted
Snakefile suggested the opposite by naming a cluster profile. One machine is what makes a shared
genome copy possible at all: one segment, loaded once, attached by every mapping job, so N concurrent
jobs cost one index instead of N. A fanned-out run would satisfy every dependency edge in the DAG and
still load a private copy per job, because an edge guarantees ORDER and never co-location. Allocation
of the whole machine comes with it — the jobs of one run are the only claimants on the memory that
run was given.

A mapping job's memory figure is therefore **self-sufficient**: it covers everything that one job
needs, index included, as though it were the only job on the machine. That is what keeps the sort cap
at three quarters of the request, since three quarters of an *incremental* figure would be smaller
than the sort it is meant to cover. It also settles what limits concurrency. N jobs sharing a copy
cost one index plus N working sets, a sum no per-job number can express, so a memory-based limit
charges the index N times and under-admits every time — which is how a 784-sample compile enforcing
the declared figure admitted two samples on a 96 GB machine, turning a ~6 hour run into a ~65 hour
one, and had to cap by **cores** instead.

Two prices, both accepted. A container that walls off shared memory makes every job load privately
with no error anywhere; the cost is speed and never correctness, and the setting belongs to the
operator submitting the pipeline rather than to a file we generate. And two runs against one assembly
on one machine can hold two copies rather than one — the copy is keyed by the genome directory, so
the second run's defensive clear marks the first run's live copy and the first run's next mapping job
creates a second instead of attaching. The worst case is two copies, which is exactly what happens
today with no sharing at all. Detecting a fanned-out run and degrading to private copies lost: STAR
reports nothing that distinguishes the two, so the check could never fire. Correcting the emitted run
instruction rather than deleting it lost for a plainer reason — how a pipeline is submitted is the
operator's business, and guidance baked into a generated file drifts from practice.
