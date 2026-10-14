# 49. STAR's command line has one owner, and the instrument runs it

STARsolo's argv was rendered twice: in `starsolo.smk`'s `shell:` block, and by hand in
`e2e.run_starsolo`, the memory instrument that reaps STAR itself because under snakemake `ru_maxrss`
folds in descendants. A Snakefile is not importable, so nothing could compare them but a substring
search over its text. They had drifted: the instrument could not run a `CB_UMI_Complex` chemistry,
and it omitted nine shipped flags, `--limitBAMsortRAM` among them, so the peak-RSS figures that
provision a 128 GB job came from an argv missing the flag that bounds STAR's memory.

`workflows/starsolo_args.py` now owns the command line and both consumers call it. The `shell:` block
is `STAR {params.argv}`, with nowhere left to drop a flag from. The instrument differs only where a
measurement forces it: `--sysShell` (a `#!` on macOS), its own `--outFileNamePrefix`, and
`--outSAMtype`, the axis `kb e2e-cost --out-sam-type` sweeps. Rejected: keep the flags in the shell
block and feed only their values from Python. The bug was flags missing, not wrong, and that still
spells the list twice.

Two prices, accepted. The Snakefile no longer shows what STAR runs; `snakemake -n -p` renders it, as
compose's wiring gate sees it. And the sweep pays for the parity set, so both figures in
[`e2e-gate-runs.md`](../research/e2e-gate-runs.md) became floors pending re-measurement.

One flag stayed behind: `--limitBAMsortRAM` escalates per retry, and snakemake re-expands
`resources:` per attempt but not `params:`, nor braces inside a substituted params value. It reaches
the shell as `SORT_CAP_SHELL`.

**Status.** Closes #348 items 2 and 3. `required_config` is a union of the regex-scanned Snakefile
(`keys_read_by`) and the AST-walked renderer (`argv_keys_read_by`), which sees both geometry branches
and ignores prose; that dropped a bare `solo` a docstring had supplied. **Amended 2026-08-11 (#370):**
the figures were ceilings, not floors, since `--limitBAMsortRAM` caps the peak. **Amended 2026-08-18
(#434):** the plate twins' argv gains bare `--outSAMunmapped Within`, never `Within KeepPairs`, whose
second token orders mates in unsorted output only. Without it the counter's first read fate could not
execute; the retained archive names the `mapped` selection.

**Amended 2026-08-21 (#463):** the decision generalises, in `splice_args.py`. STAR's junction flags
(`--outFilterType BySJout`, the two overhang minimums, `jM jI` on the attribute list) are identical in
all four STAR modules, and copying one list into each is the failure already priced. Two owners, split
by what a list is: STARsolo's argv is one module's command line, the junction flags one decision
shared by four. The three inline modules take them as one `params:` slot, `starsolo_argv` calls the
same renderer, and `SAM_WRITE_PATH` splices the attributes into each module's own list.
