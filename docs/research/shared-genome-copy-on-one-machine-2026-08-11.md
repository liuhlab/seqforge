# Do concurrent STAR jobs actually share one genome copy?

Measured 2026-08-11 on a chimera GPU node (48 cores, 1 TB RAM, native `align-rna` pixi env, no
container), against the mm10 `gencode_vM23` STAR index — 26.8 GB on disk. **Yes. Three concurrent
mapping jobs attached to one 26.7 GB segment; each held 0.42 GB resident against 26.9 GB mapped, and
no job ever created a second copy.** The lifecycle
[#379](https://github.com/liuhlab/seqforge/issues/379) shipped had until now only ever been *planned*
by `snakemake -n` for `map/star` and `map/starsolo` — never executed anywhere.

[ADR-0051](../../src/seqforge/compose/docs/adr/0051-a-composed-pipeline-runs-on-one-machine.md) is
the decision this measures, and the spec that produced it
([#375](https://github.com/liuhlab/seqforge/issues/375)) listed exactly this as *deliberately not
tested* — it would need a slow, separately-marked seam and hardware the suite does not have. So this
is a measurement, taken once, by hand. It is not a gate and nothing re-runs it.

## What was run

The commands are the ones the shipped modules render, not an approximation of them: the defensive
`Remove` then `LoadAndExit` that `rule load_genome` emits, three mapping invocations carrying
`--genomeLoad LoadAndKeep` and an explicit `--limitBAMsortRAM`, and the `Remove` that
`release_genome_segment()` calls from both handlers. 400,000 read pairs per job, four threads each,
sorted-BAM output. `ipcs -m` was polled twice a second for the life of the jobs.

## Results

| claim | observed |
|---|---|
| a shared copy with no sort limit is refused | `limitBAMsortRAM=0 (default) cannot be used with --genomeLoad=LoadAndKeep`, fatal |
| the load creates one segment | 26,719,636,361 B in 30 s |
| peak simultaneous attach count | **3** |
| large segments that ever existed | **1** |
| per-job resident peak (`VmHWM`) | **0.42 GB** |
| per-job mapped size (`VmSize`) | 26.9 GB |
| machine-wide shared memory during the run | 27 GB |
| after the release | no segment owned by the user |

The two memory figures are the whole result. A job that loaded privately would show `VmHWM` at
roughly `VmSize`; these show 0.42 GB against 26.9 GB, which is a job that mapped the index and
resident-touched only its own working set. Three such jobs on this node cost ~28 GB where three
private copies would have cost ~80 GB.

## The refusal, and why the ticket order was not arbitrary

STAR's message is worth quoting because it decided a dependency:

    EXITING because of fatal PARAMETERS error: limitBAMsortRAM=0 (default) cannot be used with
    --genomeLoad=LoadAndKeep, or any other shared memory options

It fires during parameter validation, before the genome directory is opened — so it is every sample
on the first attempt, not a slow sample now and then.
[#377](https://github.com/liuhlab/seqforge/issues/377) gave `map/star` a sort limit and
[#379](https://github.com/liuhlab/seqforge/issues/379) gave it the shared copy, in that order. Landed
the other way round, every bulk sample would have died immediately.

## What this does not cover

**A container that walls off shared memory.** This ran natively. Apptainer does not namespace IPC by
default, but `--ipc` would, and under it every job would load privately with no error anywhere —
speed lost, correctness intact. ADR-0051 records that as accepted and it stays accepted; this
measurement says nothing about it.

**Two runs against one assembly.** Also recorded as accepted, also untested here: the second run's
defensive clear marks the first run's live copy, so the first run's next mapping job creates a second
rather than attaching. The worst case is two copies, which is what happened before any sharing
existed.

**Anything about sizing.** No `mem_gb` figure was validated or challenged here, and no conclusion
about how much memory a mapping job should ask for follows from these numbers. A per-process
high-water mark over an already-shared copy answers a different question than the one `resources.mem_gb`
asks — see the module docstring in `workflows/memory.py`.

## Method

One shell script, run once through the `align-rna` environment on a node held by an existing
interactive job. It wrote only under the user's scratch root and released the segment from an
`EXIT`/`INT`/`TERM` trap, so no path through it could strand 27 GB on a shared node. The scratch
directory was removed afterwards and `ipcs -m` confirmed clean.

A first attempt sampled `ipcs -m` once, 25 seconds after starting three 200-read jobs, and read
`nattch 0` — the jobs had finished and detached inside one second. The input was enlarged until the
jobs outlived the sampling interval. Recorded because the trap is easy to fall into again: an attach
count is only meaningful while something is attached.
