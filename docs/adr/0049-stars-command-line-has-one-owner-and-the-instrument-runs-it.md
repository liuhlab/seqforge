# 49. STAR's command line has one owner, and the instrument runs it

STARsolo's argv was rendered twice — once in `starsolo.smk`'s `shell:` block, once by hand in
`e2e.run_starsolo`, the memory instrument that must reap STAR itself because under snakemake
`ru_maxrss` folds in descendants. Nothing could compare them: a Snakefile is not importable, so the
only expression available was a whitespace-normalised substring search over the module's *text*. The
two had drifted twice over. The instrument named the four simple-geometry flags directly, so it could
not run a `CB_UMI_Complex` chemistry at all; and it omitted **nine** shipped flags, `--limitBAMsortRAM`
among them — the memory instrument left out the flag that bounds STAR's memory, and the peak-RSS
figures that provision a 128 GB job came from that argv. Neither omission was a decision anyone took.

`workflows/starsolo_args.py` now owns the command line and both consumers call it. The `shell:` block
is `STAR {params.argv}`, so a flag cannot be dropped there because there is nowhere left to drop one
from, and the instrument may differ only where a measurement physically forces it: `--sysShell` (STAR's
generated reader needs a `#!` on macOS), its own `--outFileNamePrefix`, and `--outSAMtype`, the axis
`kb e2e-cost --out-sam-type` exists to sweep. The alternative — keep the flags visible in the shell
block and feed only their *values* from Python — was rejected because it fixes the wrong failure: the
bug was nine flags **missing**, not wrong, and under it the flag list is still spelled twice.

Two prices, both accepted. The Snakefile no longer shows what STAR runs; `snakemake -n -p` renders it
in full, which is also how compose's wiring gate sees it, and the reasoning for each literal moved to
sit beside the literal. And the sweep now pays for the parity set at every point, so both figures in
[`e2e-gate-runs.md`](../research/e2e-gate-runs.md) became floors pending a re-measurement on arc.

One flag stayed behind, measured rather than chosen: `--limitBAMsortRAM` must escalate per retry
attempt, and snakemake re-expands `resources:` per attempt but not `params:` — nor does it expand
braces arriving *inside* a substituted params value, since the shell template is formatted in one
pass. It reaches the shell as `SORT_CAP_SHELL`, which spells the flag so the Snakefile does not.

**Status.** Closes #348 items 2 and 3. `required_config` stays derived and is now a union: the
Snakefile is still regex-scanned (`keys_read_by`) and the renderer is AST-walked
(`argv_keys_read_by`), which is strictly more precise — it sees both geometry branches where a scanner
could only see the one it took, and it ignores prose, which the regex did not. That last point cost a
key: the old scan reported a bare `solo` because a docstring mentioned `config["solo"]`. **Amended
2026-08-11 (#370):** the re-measurement landed and the two figures were not floors but ceilings —
`--limitBAMsortRAM` is a cap, so adding it lowered the peak. The decision stands; only the guess
about which way the unmeasured error ran was wrong.
