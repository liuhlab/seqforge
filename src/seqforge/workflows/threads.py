"""What one rule asks the scheduler for in CORES, out of the recipe's ONE threads figure.

The recipe says exactly one thing about threads, on purpose: `resources.threads` is INTENT, and a
recipe carrying every module's rule names would widen the recipe schema on every new module. So the
module -- the only artifact that knows its own rule graph -- turns that one figure into the requests
its own rules need. The same move `workflows.memory` makes over `resources.mem_gb`, made for the same
reason, which is also why it lives beside that file rather than inside a `.smk`: a Snakefile is not
importable, so a figure written there can be exercised only by running the rule it decorates.

**This map is deliberately PARTIAL, and that is the discipline rather than an omission.** A rule
gets an entry here only where a measurement or a certainty stands behind the number; every other
rule keeps `config["threads"]` until one does. A per-rule constant invented to make the map look
complete is worse than the recipe's figure, because it reads as evidence. `unique_to_cram` is the
case that settles it: 4.0 cores on one Component and 1.2 on another, same rule and same declaration,
so the difference is the DATA and no constant can express it. `star_umi_map`, `umi_extract`,
`load_genome` and `multiplaced_to_cram` are outside for the plainer reason that nothing has been
measured on the code that ships.

**Do not pick a figure here off a utilisation ratio.** Total CPU is flat across a 1/2/4/8/16 sweep of
the chimeric split -- 14.53, 14.54, 14.63, 14.86 and 15.13 core-seconds -- so a 16x change in the
reservation moves the work done by 4%, and cores-per-declared-thread is that fixed work divided by a
wall clock: 1.05, 0.54, 0.35, 0.51, 0.33, non-monotonic, with a trough at 4 and a bump at 8 that no
property of the rule explains. *This rule reserves eight cores and converts them into 2.4* is a true
sentence about a ratio whose denominator is a declaration and whose numerator is a wall clock, and it
moves when either end moves for reasons that have nothing to do with whether the reservation is
right. What decides a figure here is a wall-time gain per doubling, or a measured peak.

**One trap worth recording because it looks like a solution.** `threads: 999` is silently clamped to
`--cores` under local execution, so "take the whole machine" appears to work. It is not portable --
under any executor it becomes a job requesting 999 CPUs that no partition can satisfy -- which is why
:func:`fan_in_threads` is relative to the run's own width rather than a large constant.
"""

from __future__ import annotations


def fan_in_threads(cores: int) -> int:
    """What the plate-wide counting job asks for: the RUN'S WIDTH, less one for the parent.

    The argument is `workflow.cores`, the width the operator gave this snakemake instance, and the
    fan-in takes all of it but one. Minus ONE because the counter forks a worker per thread and the
    PARENT stays running: it accumulates each cell's matrix as the worker hands it back, so a worker
    on the parent's core contends with the process assembling the object rather than with nothing.

    **This is a legible number only because a snakemake instance owns one machine (ADR-0051).** The
    fan-in is the last job of the instance -- every cell has finished by the time it can start -- so
    the instance's width is exactly what is free at that moment, and there is nothing else of the
    run's to leave room for. Under a fanned-out DAG the same expression would name the width of
    whichever machine parsed the Snakefile and mean nothing anywhere else, which is why the record
    narrowing its unit to an instance is a precondition for this declaration and not a consequence
    of it.

    What it replaces is the recipe's figure, which is sized for a MAPPING job: on a 160-core node the
    plate-wide count ran 16 workers at ~10% utilisation with nothing else on the machine, and on a
    16-core node the same figure would have been half of it. Neither is a property of this rule. A
    constant cannot be right on both machines and a relative width is right on either.

    **The escape hatch survives**: `--set-threads umi_count=N` overrides this exactly as it overrode
    a constant, which is what rescued the 784-cell plate mid-run.

    **Naming the run's width makes `--cores` MANDATORY for a module carrying this rule.** Snakemake
    refuses to build a DAG that reads a core count it was never given -- so a planner that used to
    get away with omitting the flag now has to name a width, the wiring gate included. That is the
    one cost of the relative form, and it is paid at DAG build with snakemake's own sentence rather
    than at run time with a wrong number.

    The floor of one is for the degenerate `--cores 1`, where the parent is the only process there
    is: a rule may not ask for zero threads, and the counter still runs, with its single worker
    sharing the parent's core.

    Its memory follows and is already paid for -- see `workflows.memory.fan_in_mem_mb`, which covers
    a worker's ~260 MB out to far more workers than a node has cores.
    """
    return max(1, cores - 1)


#: What one cell's QC fold asks for: ONE core, measured at 0.8. `io qc-bundle` reads a handful of
#: small text files a cell already wrote and folds them into one gzipped JSON; nothing in it is
#: parallel and nothing in it is large.
#:
#: **It is also what snakemake assumes for a rule that declares nothing, so this changes no plan.**
#: It is written down because a partial map is only readable if the holes mean something: an
#: undeclared rule and a rule measured at one core look identical from the outside, and the whole
#: point of this file is that a figure in it points at evidence. Declaring the one says the one is a
#: measurement.
QC_BUNDLE_THREADS: int = 1

#: The ceiling on the chimeric split, in cores: above this the rule provably cannot spend what it is
#: given. The wall-time gain per doubling on a 1M-record fixture is 0.47 s (1 -> 2), 2.83 s (2 -> 4),
#: **6.93 s (4 -> 8)** and 0.82 s (8 -> 16) -- the largest gain in the sweep is the one that ends
#: here, and the next doubling buys 0.82 s for twice the reservation because ~83% of the wall at 16
#: is the serial routing loop, which no further thread touches. Declaring 4 instead would leave
#: 6.9 s per cell on the table, 1.9x the entire wall at 8.
#:
#: An UPPER bound rather than a central estimate: the knee sits where the dominant Component's byte
#: share divided by its writer's worker count crosses the loop floor, and the fixture that placed it
#: splits 90/10 by output bytes on a warm local SSD with a synthetic cell. On a cold or networked
#: filesystem the decompression share grows and the loop floor arrives sooner, so the knee moves
#: DOWN; a lower compression level moves it too.
_SPLIT_CHIMERA_MAX_THREADS = 8


def split_chimera_threads(threads: int) -> int:
    """What one cell's chimeric split asks for: the SMALLER of the recipe's figure and the ceiling.

    A ceiling, not a constant, and the two halves are separate promises. It never asks for more than
    the operator budgeted, because `resources.threads` is the operator's statement about the machine
    and a rule that walks past it is not reading the recipe. And it never asks for more than it can
    use, because the reservation above :data:`_SPLIT_CHIMERA_MAX_THREADS` buys a wall-clock gain the
    sweep can barely see.

    **A no-op at today's default**, where the recipe's figure and the ceiling are both 8. It starts
    mattering the moment a recipe raises `threads` for a deep sample -- which is exactly the run
    where the rule would otherwise hold cores it provably cannot spend, on every cell of the plate at
    once, while the mapping jobs that CAN spend them wait for a slot.
    """
    return min(threads, _SPLIT_CHIMERA_MAX_THREADS)


__all__ = [
    "QC_BUNDLE_THREADS",
    "fan_in_threads",
    "split_chimera_threads",
]
