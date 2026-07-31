# 15. Barcode whitelists are built by a rule and `temp()`-deleted, never stored

Date: 2026-07-31

## Status

Accepted.

## Context

`compose` used to expand the resolved barcode whitelist into every run directory at compile time.
10x's v3 list is **6 794 880 barcodes = 111 MB of text**, so one dataset compiled three ways cost a
third of a gigabyte of identical bytes, sitting there forever, for a file STAR opens **once**.

It was `temp()`-able in name only: the whitelist was bound to `starsolo_count.input` with **no
producing rule**, and snakemake cannot delete a file it did not make. An input with no rule is a
file snakemake merely requires to already be there.

## Decision

`rule onlist` materializes one whitelist on demand (`seqforge io onlist write`), STAR reads it, and
`temp()` deletes it when the last job that needs it is done. **Onlists are not part of the on-disk
state under `seqforge/`** — the shipped packed array is the only stored copy.

## Why not cache the expanded text

The expansion is a pure function of a packed array we already ship. A barcode is 2 bits per base, so
the v3 list is **522 kB packed** against 12 MB as a `.txt.gz` and 111 MB expanded. Caching the
expansion trades cheap, deterministic CPU for expensive duplicated bytes that no cache key protects
and no one ever collects.

## So in code

**Never write an expanded barcode whitelist into a run directory, or anywhere under `seqforge/`.**
Bind it to `rule onlist`, which materializes it on demand and `temp()`-deletes it once the last job
that needs it is done; the shipped packed array stays the only stored copy. A `temp()` on an input
with no producing rule does nothing — snakemake cannot delete a file it did not make. Declare no
`container:` on that rule: it runs `seqforge`, not an aligner. And when adding an aligner module,
reuse the same rule rather than writing a variant — a barcode whitelist is a barcode whitelist.

**Enforced by.** `test_the_whitelist_is_a_rule_output_not_a_compile_time_write`
(`tests/test_compose.py`);
`test_no_run_directive_rule_declares_a_container` (same file) for the missing `container:`;
`test_the_composed_pipeline_plans_the_h5ad_the_whitelist_and_the_barcode_read_length` (same file),
which is what fails if the wiring gate stops touching the resolved-onlist cache paths.

## Consequences

- A run directory stays small and reads as **the deliverable** — a Snakefile, a config, a units
  file, a workflow module — rather than a data store.
- The `compose` wiring gate must touch zero-byte files at the resolved-onlist cache paths as well as
  at every path in the file inventory, or `snakemake -n` raises a spurious `MissingInputException`
  for a whitelist declared as an `input:`.
- `rule onlist` carries **no `container:` directive**, deliberately: it runs `seqforge`, which is
  not an aligner. The ambient environment is the one that just ran `seqforge compose`, so by
  construction it has the tool; naming `align-rna` there would put our own tool inside STAR's
  image.
- `chromap.smk` uses the byte-identical rule — a barcode whitelist is a barcode whitelist, and the
  discipline is not STARsolo-specific.
